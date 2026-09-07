"""
Проверка идеи "Револьверная+Трендовая одновременно, не переключение по режиму"
(2026-09-07, продолжение "мировой практики" — Balvers & Wu: комбинация momentum+
mean-reversion бьёт каждую по отдельности). Реальные правила ОБЕИХ стратегий:

Револьверная (вход _score_revolver, выход K_stop=6/K_trail=5/target+6%/confirm=12d/
timeout=30d) -- как в k_vol_exit_long_run.py, тема №175.

Трендовая (вход _score_trend, analytics/analytics_utils.py:635): оборот>=$100М,
веер EMA20>SMA50>SMA100, цена>SMA200+5%, RSI 50-72, MACD>0, ranking=price_to_sma200/vol
(потолок здравого смысла 30.0). Выход (_check_trend_exit, position_exit_evaluator.py):
подтверждённый слом (streak<=-3d) ИЛИ EMA20<SMA50 ИЛИ (RSI>80 И MACD<0) -- без
фиксированной цели/тайм-аута, держит пока тренд жив.

Нужна ДЛИННАЯ история для устойчивой SMA200 (200 торговых дней ~9.5 месяцев) --
существующие кэши экономили на предыстории (буфер 2-3 месяца), качать заново
с запасом от 2024-01-01.

Три сценария на капитал $20 000: 100% Револьверная, 100% Трендовая, 50/50 сплит
(своя структура слотов у каждой: Револьверная $1000×N, Трендовая $3000×N) --
сравнение доходность+просадка+VTI.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import settings
import backtest as bt

CACHE_DIR = Path(__file__).resolve().parent / '_cache'
FRAMES_CACHE = CACHE_DIR / 'signal_frames_combined_sleeves.pkl'

with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
symbols = list(fundamentals.keys())

DOWNLOAD_START = '2024-01-01'   # ~13 мес буфера до начала теста (2025-02-01) -- хватает на SMA200
DOWNLOAD_END = '2026-09-06'
TEST_START, TEST_END = date(2025, 2, 1), date(2026, 9, 5)

try:
    with open(FRAMES_CACHE, 'rb') as f:
        frames = pickle.load(f)
    print(f"Кэш найден: {len(frames)} тикеров")
except FileNotFoundError:
    print(f"Скачиваю историю {DOWNLOAD_START} -> {DOWNLOAD_END} (нужен запас для SMA200)...")
    raw = yf.download(symbols, start=DOWNLOAD_START, end=DOWNLOAD_END, interval='1d',
                       group_by='ticker', threads=True, progress=False, auto_adjust=False)
    frames = {}
    for sym in symbols:
        try:
            df = raw[sym].dropna(subset=['Close'])
        except Exception:
            continue
        if df is None or len(df) < 220:
            continue
        close, vol, high, low = df['Close'], df['Volume'], df['High'], df['Low']
        ema20 = close.ewm(span=20, adjust=False).mean()
        sma50 = close.rolling(50).mean()
        sma100 = close.rolling(100).mean()
        sma200 = close.rolling(200).mean()
        price_to_sma200_pct = (close / sma200 - 1) * 100
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta.where(delta < 0, 0))
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
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
            'close': close, 'high': high, 'low': low, 'rsi': rsi, 'macd': macd, 'vol_pct': vol_pct,
            'streak': streak, 'vol_ratio_20d': vol_ratio_20d, 'price_to_20d_high_pct': price_to_20d_high_pct,
            'turnover_usd': turnover_usd, 'ema20': ema20, 'sma50': sma50, 'sma100': sma100,
            'sma200': sma200, 'price_to_sma200_pct': price_to_sma200_pct,
        })
    with open(FRAMES_CACHE, 'wb') as f:
        pickle.dump(frames, f)
    print(f"Готово, {len(frames)} тикеров")

trading_days = [d.date() for d in next(iter(frames.values())).index if TEST_START <= d.date() <= TEST_END]

# ── Револьверная (реальные параметры, тема №175) ──
REV_K_STOP, REV_K_TRAIL, REV_TARGET, REV_CONFIRM, REV_TIMEOUT = 6.0, 5.0, 6.0, 12, 30
REV_SLOT = 1000.0

# ── Трендовая (реальные параметры, _score_trend/_check_trend_exit) ──
TREND_TURNOVER_MIN = 100_000_000.0
TREND_RSI_LOW, TREND_RSI_HIGH = 50.0, 72.0
TREND_RATIO_CEILING = settings.TREND_MOMENTUM_RATIO_CEILING
TREND_CONFIRM_DAYS = 3  # settings.TREND_REVERSAL_CONFIRM_DAYS
TREND_RSI_OVERHEAT = 80.0
TREND_SLOT = 3000.0


def screen_revolver(check_date):
    return bt.screen_at_date(check_date, fundamentals, frames)


def screen_trend(check_date, exclude_syms):
    ts = pd.Timestamp(check_date)
    out = []
    for sym, df in frames.items():
        if sym in exclude_syms:
            continue
        idx = df.index[df.index <= ts]
        if len(idx) == 0:
            continue
        last_ts = idx[-1]
        if (check_date - last_ts.date()).days > 5:
            continue
        row = df.loc[last_ts]
        turnover = float(row['turnover_usd']) if not np.isnan(row['turnover_usd']) else 0.0
        ema20, sma50, sma100 = row['ema20'], row['sma50'], row['sma100']
        if np.isnan(ema20) or np.isnan(sma50) or np.isnan(sma100):
            continue
        fan_valid = (ema20 > sma50) and (sma50 > sma100)
        price_to_sma200 = float(row['price_to_sma200_pct']) if not np.isnan(row['price_to_sma200_pct']) else -999
        rsi = float(row['rsi']) if not np.isnan(row['rsi']) else 0.0
        macd = float(row['macd']) if not np.isnan(row['macd']) else 0.0
        vol = row['vol_pct']
        ranking = (price_to_sma200 / vol) if (vol and not np.isnan(vol) and vol > 0) else price_to_sma200
        checks = [
            turnover >= TREND_TURNOVER_MIN, fan_valid, price_to_sma200 > 5.0,
            TREND_RSI_LOW <= rsi <= TREND_RSI_HIGH, macd > 0, abs(ranking) <= TREND_RATIO_CEILING,
        ]
        if all(checks):
            out.append((sym, ranking, float(row['close'])))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


class Slot:
    def __init__(self, kind, size):
        self.kind = kind  # 'REV' или 'TREND'
        self.size = size
        self.symbol = None
        self.value = size


def run(n_rev_slots, n_trend_slots):
    slots = [Slot('REV', REV_SLOT) for _ in range(n_rev_slots)] + [Slot('TREND', TREND_SLOT) for _ in range(n_trend_slots)]
    trades_log = []
    rev_screen_cache, trend_screen_cache = {}, {}
    daily_portfolio_value = []

    for day in trading_days:
        ts = pd.Timestamp(day)
        for s in slots:
            if s.symbol is None:
                continue
            df = frames[s.symbol]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            close = float(row['close'])
            exit_price, reason = None, None

            if s.kind == 'REV':
                low, high = float(row['low']), float(row['high'])
                vol = float(row['vol_pct']) if not np.isnan(row['vol_pct']) else 0.0
                streak = row['streak']
                days_held = (day - s.entry_date).days
                s.peak = max(s.peak, high)
                if vol > 0:
                    sl_price = s.entry_price * (1 - REV_K_STOP * vol / 100.0)
                    if low <= sl_price:
                        exit_price, reason = sl_price, 'REV_SL'
                    elif s.peak > s.entry_price * 1.005:
                        trail_price = s.peak * (1 - REV_K_TRAIL * vol / 100.0)
                        if low <= trail_price:
                            exit_price, reason = trail_price, 'REV_TS'
                if exit_price is None:
                    profit_pct = (close - s.entry_price) / s.entry_price * 100.0
                    if profit_pct >= REV_TARGET:
                        exit_price, reason = close, 'REV_TP'
                    elif streak is not None and not np.isnan(streak) and streak <= -REV_CONFIRM:
                        exit_price, reason = close, 'REV_BREAK'
                    elif days_held >= REV_TIMEOUT:
                        exit_price, reason = close, 'REV_TIMEOUT'
            else:  # TREND
                ema20, sma50 = row['ema20'], row['sma50']
                rsi = float(row['rsi']) if not np.isnan(row['rsi']) else 0.0
                macd = float(row['macd']) if not np.isnan(row['macd']) else 0.0
                streak = row['streak']
                if streak is not None and not np.isnan(streak) and streak <= -TREND_CONFIRM_DAYS:
                    exit_price, reason = close, 'TREND_BREAK'
                elif not np.isnan(ema20) and not np.isnan(sma50) and ema20 < sma50:
                    exit_price, reason = close, 'TREND_FAN'
                elif rsi > TREND_RSI_OVERHEAT and macd < 0:
                    exit_price, reason = close, 'TREND_OVERHEAT'

            if exit_price is not None:
                gross_pct = (exit_price - s.entry_price) / s.entry_price * 100.0
                net_pct = gross_pct - bt.COMMISSION_RT_PCT
                s.value *= (1 + net_pct / 100.0)
                trades_log.append({'reason': reason, 'net_pct': net_pct, 'kind': s.kind})
                s.symbol = None

        empty_rev = [s for s in slots if s.symbol is None and s.kind == 'REV']
        empty_trend = [s for s in slots if s.symbol is None and s.kind == 'TREND']
        held = {s.symbol for s in slots if s.symbol}
        if empty_rev:
            if day not in rev_screen_cache:
                rev_screen_cache[day] = screen_revolver(day)
            cands = [c for c in rev_screen_cache[day] if c[0] not in held]
            for s in empty_rev:
                if not cands:
                    break
                sym, rank, price = cands.pop(0)
                s.symbol, s.entry_date, s.entry_price, s.peak = sym, day, price, price
                held.add(sym)
        if empty_trend:
            cands = screen_trend(day, held)
            for s in empty_trend:
                if not cands:
                    break
                sym, rank, price = cands.pop(0)
                s.symbol, s.entry_date, s.entry_price = sym, day, price
                held.add(sym)

        mtm_total = 0.0
        for s in slots:
            if s.symbol is None:
                mtm_total += s.value
                continue
            df = frames[s.symbol]
            if ts in df.index:
                mtm_total += s.value * (float(df.loc[ts]['close']) / s.entry_price)
            else:
                mtm_total += s.value
        daily_portfolio_value.append(mtm_total)

    total_end = sum(s.value for s in slots)
    total_start = sum(s.size for s in slots)
    values = pd.Series(daily_portfolio_value)
    max_dd = ((values - values.cummax()) / values.cummax() * 100.0).min()
    df = pd.DataFrame(trades_log)
    return {
        'return_pct': (total_end / total_start - 1) * 100, 'n': len(df),
        'max_dd': max_dd, 'trades': df, 'total_start': total_start,
    }


if __name__ == '__main__':
    print(f"Период: {TEST_START} -> {TEST_END}\n")
    scenarios = [
        ('100% Револьверная (20 слотов $1000)', 20, 0),
        ('100% Трендовая (~7 слотов $3000)', 0, 6),
        ('50/50: 10 Револьверной + 3 Трендовой', 10, 3),
    ]
    for label, n_rev, n_trend in scenarios:
        r = run(n_rev, n_trend)
        print(f"=== {label} (старт ${r['total_start']:,.0f}) ===")
        print(f"  Доходность={r['return_pct']:+.2f}%  Просадка={r['max_dd']:+.2f}%  Сделок={r['n']}")
        if len(r['trades']):
            for reason, g in r['trades'].groupby('reason'):
                print(f"    {reason:14} n={len(g):3}  net_среднее={g['net_pct'].mean():+6.2f}%")
        print()

    vti = yf.Ticker('VTI').history(start=str(TEST_START), end=str(TEST_END + timedelta(days=1)))
    vti_ret = (vti['Close'].iloc[-1] / vti['Close'].iloc[0] - 1) * 100
    print(f"VTI buy-and-hold: {vti_ret:+.2f}%")
