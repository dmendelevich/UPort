"""
Разведочный бэктест: жёсткий флэт-% SL/TP вместо confirm_days-подтверждения слома
тренда для Револьверной стратегии. Универс -- MS_SP500 (текущий состав), вход --
реальные гейты _score_revolver (analytics/analytics_utils.py) на исторических ценах,
выход -- SL/TP параметрами ниже + тайм-аут позиции. Согласовано и прогнано в сессии
2026-09-05 (Claude/BACKLOG.md #167) -- этот файл существует, чтобы прогон был
воспроизводим, а не одноразовым (см. урок из более ранней 8.3-летней проверки,
скрипт которой не сохранился и не смог быть сверен заново).

Известные упрощения эксперимента (см. BACKLOG.md #167 для полного разбора):
1. Фундаментал (FCF/рост выручки/дивдоходность/консенсус аналитиков) -- ТЕКУЩИЙ
   снимок tickers, не исторический на дату чек-пойнта (yfinance не отдаёт
   точечно-во-времени фундаментал) -- лёгкий lookahead bias, для крупных
   стабильных SP500-имён за 4-5 месяцев обычно небольшой.
2. Универс -- текущий состав MS_SP500, не историческое членство на дату проверки.
3. Без секторных лимитов (25% на сектор, как в живой системе) -- если увидим явный
   перекос, стоит добавить отдельным шагом.
4. Флэт-% SL/TP, не K x волатильность (обычный принцип проекта) -- сознательно для
   интерпретируемости первого прохода; сеточный тест (BACKLOG #167) показал, что
   это может давать искажения на очень волатильных именах -- следующий шаг, если
   тема продолжится, вероятно, K x vol.
5. entry_trigger "trend_not_confirmed_broken" (confirm_days) НА ВХОДЕ сохранён как
   есть (не путать с confirm_days НА ВЫХОДЕ, которую по этой теме сознательно
   убрали) -- см. обсуждение в сессии.
6. Момент "цель достигнута, момент жив -> перенос в Трендовую" НЕ моделируется --
   любое достижение TP считается чистой зафиксированной прибылью для слота
   (согласовано пользователем -- "эффективность считаем только для револьверной").

Запуск: python3 backtest.py (нужен venv проекта, доступ к БД для фундаментала).
Параметры -- константы в начале файла, менять и перезапускать.
"""
import sys
import json
import warnings
import pickle
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # корень UPort

import numpy as np
import pandas as pd
import yfinance as yf
import settings
from database import db_sys

# ─────────────────────────────────────────────────────────
# ПАРАМЕТРЫ ЭКСПЕРИМЕНТА -- менять здесь
# ─────────────────────────────────────────────────────────
SL_PCT = 10.0
TP_PCT = 10.0
TIMEOUT_DAYS = 10
RESCAN_DAYS = 10          # держать равным TIMEOUT_DAYS -- см. обсуждение "слот не должен простаивать"
N_SLOTS = 10
SLOT_USD = 1000.0
START_DATE = datetime(2026, 4, 1).date()
END_DATE = None            # None = сегодня

CONFIRM_DAYS = 12          # tactic_confirm_days Револьверной -- только для ВХОДА (гейт "не покупай уже сломанное")
DEPTH_CEILING = settings.REVOLVER_DEPTH_RATIO_CEILING
LIMIT_TURNOVER = 500_000_000.0
LIMIT_RSI = 45.0
LIMIT_GROWTH = 0.00
LIMIT_DIV = 1.5
LIMIT_VOL_RATIO = 1.0

TARIFF_PCT = 0.0012        # П10: 0.12% от суммы сделки
TARIFF_FIXED = 1.2         # $ за приказ
COMMISSION_RT_PCT = (2 * (TARIFF_PCT * SLOT_USD + TARIFF_FIXED)) / SLOT_USD  # round-trip на $1000 слот

CACHE_DIR = Path(__file__).resolve().parent / '_cache'
CACHE_DIR.mkdir(exist_ok=True)


def load_universe_and_history():
    fundamentals_path = CACHE_DIR / 'sp500_fundamentals.json'
    frames_path = CACHE_DIR / 'signal_frames.pkl'

    if fundamentals_path.exists():
        fundamentals = {r['symbol']: r for r in json.loads(fundamentals_path.read_text())}
    else:
        rows = db_sys.execute_query('''
            SELECT symbol, free_cash_flow, revenue_growth, dividend_yield, recommendation_mean
            FROM public.tickers WHERE provenance ? 'MS_SP500';
        ''')
        fundamentals_path.write_text(json.dumps(rows))
        fundamentals = {r['symbol']: r for r in rows}

    if frames_path.exists():
        with open(frames_path, 'rb') as f:
            frames = pickle.load(f)
        return fundamentals, frames

    symbols = list(fundamentals.keys())
    end = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    raw = yf.download(symbols, start='2026-01-01', end=end, interval='1d',
                       group_by='ticker', threads=True, progress=False, auto_adjust=False)

    frames = {}
    for sym in symbols:
        try:
            df = raw[sym].dropna(subset=['Close'])
        except Exception:
            continue
        if df is None or len(df) < 60:
            continue
        close, vol = df['Close'], df['Volume']
        ema20 = close.ewm(span=20, adjust=False).mean()
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta.where(delta < 0, 0))
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 0.00001)
        rsi = 100 - (100 / (1 + rs))
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        vol_pct = close.pct_change().rolling(settings.DAILY_VOLATILITY_WINDOW_DAYS).std() * 100
        above = (close > ema20)
        grp = (above != above.shift()).cumsum()
        streak_raw = above.groupby(grp).cumcount() + 1
        streak = np.where(above, streak_raw, -streak_raw)
        vol_ratio_20d = vol / vol.rolling(20).mean()
        high_20d = close.rolling(20).max()
        price_to_20d_high_pct = (close / high_20d - 1) * 100
        turnover_usd = vol * close

        frames[sym] = pd.DataFrame({
            'close': close, 'rsi': rsi, 'macd': macd, 'vol_pct': vol_pct,
            'streak': streak, 'vol_ratio_20d': vol_ratio_20d,
            'price_to_20d_high_pct': price_to_20d_high_pct, 'turnover_usd': turnover_usd,
            'high': df['High'], 'low': df['Low'],
        })

    with open(frames_path, 'wb') as f:
        pickle.dump(frames, f)
    return fundamentals, frames


def na_check(raw, fn):
    return True if raw is None else fn(float(raw))


def screen_at_date(check_date, fundamentals, frames):
    ts = pd.Timestamp(check_date)
    candidates = []
    for sym, df in frames.items():
        idx = df.index[df.index <= ts]
        if len(idx) == 0:
            continue
        last_ts = idx[-1]
        if (check_date - last_ts.date()).days > 5:
            continue
        row = df.loc[last_ts]
        f = fundamentals.get(sym, {})
        turnover = float(row['turnover_usd']) if not np.isnan(row['turnover_usd']) else 0.0
        rsi = float(row['rsi']) if not np.isnan(row['rsi']) else 0.0
        macd = float(row['macd']) if not np.isnan(row['macd']) else 0.0
        streak = row['streak']
        vol_ratio = row['vol_ratio_20d']
        depth_raw = row['price_to_20d_high_pct']
        vol_for_rank = row['vol_pct']
        rec_mean = float(f.get('recommendation_mean') or 0.0)
        fcf, growth, div = f.get('free_cash_flow'), f.get('revenue_growth'), f.get('dividend_yield')

        checks = [
            turnover >= LIMIT_TURNOVER,
            rsi < LIMIT_RSI,
            macd > 0 or (0 < rec_mean <= 2.0),
            na_check(fcf, lambda v: v > 0),
            na_check(growth, lambda v: v > LIMIT_GROWTH),
            na_check(div, lambda v: v <= LIMIT_DIV),
            na_check(vol_ratio if not (isinstance(vol_ratio, float) and np.isnan(vol_ratio)) else None, lambda v: v >= LIMIT_VOL_RATIO),
            not (streak is not None and not np.isnan(streak) and streak <= -CONFIRM_DAYS),
        ]
        if depth_raw is None or np.isnan(depth_raw) or vol_for_rank is None or np.isnan(vol_for_rank) or vol_for_rank <= 0:
            depth_ranking = 0.0
        else:
            depth_ranking = -float(depth_raw) / float(vol_for_rank)
        checks.append(abs(depth_ranking) <= DEPTH_CEILING)

        if all(checks):
            candidates.append((sym, depth_ranking, float(row['close'])))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def simulate_slot(sym, entry_date, entry_price, frames):
    df = frames[sym]
    start_ts, end_ts = pd.Timestamp(entry_date), pd.Timestamp(entry_date + timedelta(days=TIMEOUT_DAYS))
    window = df[(df.index > start_ts) & (df.index <= end_ts)]
    sl_price = entry_price * (1 - SL_PCT / 100.0)
    tp_price = entry_price * (1 + TP_PCT / 100.0)
    for ts, row in window.iterrows():
        low, high = float(row['low']), float(row['high'])
        if low <= sl_price:
            return sl_price, ts.date(), 'SL'
        if high >= tp_price:
            return tp_price, ts.date(), 'TP'
    if len(window) > 0:
        last = window.iloc[-1]
        return float(last['close']), window.index[-1].date(), 'TIMEOUT'
    return entry_price, entry_date, 'NO_DATA'


def run():
    fundamentals, frames = load_universe_and_history()
    end_date = END_DATE or datetime.now().date()

    checkpoints = []
    d = START_DATE
    while d <= end_date:
        checkpoints.append(d)
        d += timedelta(days=RESCAN_DAYS)

    slot_value = [SLOT_USD] * N_SLOTS
    all_trades = []
    for cycle_n, cp in enumerate(checkpoints, 1):
        chosen = screen_at_date(cp, fundamentals, frames)[:N_SLOTS]
        for i in range(N_SLOTS):
            if i >= len(chosen):
                continue
            sym, rank, price = chosen[i]
            exit_price, exit_date, reason = simulate_slot(sym, cp, price, frames)
            gross_pct = (exit_price - price) / price * 100.0
            net_pct = gross_pct - COMMISSION_RT_PCT
            slot_value[i] *= (1 + net_pct / 100.0)
            all_trades.append({'cycle': cycle_n, 'entry_date': str(cp), 'symbol': sym, 'reason': reason, 'net_pct': net_pct})

    df = pd.DataFrame(all_trades)
    total_end = sum(slot_value)
    print(f"SL={-SL_PCT}% TP=+{TP_PCT}% timeout={TIMEOUT_DAYS}d rescan={RESCAN_DAYS}d  |  {START_DATE} -> {end_date}")
    print(f"Старт ${SLOT_USD*N_SLOTS:,.2f} -> Финиш ${total_end:,.2f}  ({(total_end/(SLOT_USD*N_SLOTS)-1)*100:+.2f}%)")
    print(f"Сделок: {len(df)}, winrate: {(df['net_pct']>0).mean()*100:.1f}%\n")
    for reason, g in df.groupby('reason'):
        wins = (g['net_pct'] > 0).sum()
        print(f"  {reason:9} n={len(g):3}  winrate={wins/len(g)*100:5.1f}%  net: мин={g['net_pct'].min():+6.2f}% "
              f"макс={g['net_pct'].max():+6.2f}% среднее={g['net_pct'].mean():+6.2f}%")
    return df


if __name__ == '__main__':
    run()
