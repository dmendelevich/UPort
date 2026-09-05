"""
Проверка на надёжность, третий эпизод (2026-09-05, Claude/BACKLOG.md #167) --
01.02-21.04.2025, по прямому запросу пользователя после того, как тест на
2023-м оказался испорчен нехваткой кандидатов (фундаментал-2026 слишком старо
применён к 2023-му, 3 года разрыва). Этот период всего на ~1.5 года старше
"сегодня" (2026), фундаментал должен быть заметно ближе к реальности того
момента, чем в тесте 2022/2023 -- честнее проверка.

VTI за этот период: -14.48% -- реально ещё один медвежий эпизод (третий по
счёту после Феб-2026 и 2022), не бычий, хотя запрошен был как проверка
надёжности вообще, не конкретно "ещё один бычий".
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
import settings
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
symbols = list(fundamentals.keys())

CACHE = str(CACHE_DIR / 'signal_frames_2025_feb_apr.pkl')
try:
    with open(CACHE, 'rb') as f:
        frames = pickle.load(f)
    print(f"Кэш найден: {len(frames)} тикеров")
except FileNotFoundError:
    print("Скачиваю историю цен за конец 2024 - апрель 2025...")
    raw = yf.download(symbols, start='2024-11-01', end='2025-05-01', interval='1d',
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
            'close': close, 'rsi': rsi, 'macd': macd, 'vol_pct': vol_pct,
            'streak': streak, 'vol_ratio_20d': vol_ratio_20d,
            'price_to_20d_high_pct': price_to_20d_high_pct, 'turnover_usd': turnover_usd,
            'high': df['High'], 'low': df['Low'],
        })
    with open(CACHE, 'wb') as f:
        pickle.dump(frames, f)
    print(f"Готово, {len(frames)} тикеров")

bt.RESCAN_DAYS = 10
bt.TIMEOUT_DAYS = 10

START_DATE = date(2025, 2, 1)
END_DATE = date(2025, 4, 21)
checkpoints = []
d = START_DATE
while d <= END_DATE:
    checkpoints.append(d)
    d += timedelta(days=bt.RESCAN_DAYS)

counts = [len(bt.screen_at_date(cp, fundamentals, frames)) for cp in checkpoints]
print(f"\nКандидатов по чек-пойнтам (01.02-21.04.2025): {counts}")
print(f"Среднее: {sum(counts)/len(counts):.1f}  медиана: {sorted(counts)[len(counts)//2]}  мин/макс: {min(counts)}/{max(counts)}")
print(f"Циклов с <5 кандидатами: {sum(1 for c in counts if c<5)} из {len(counts)}")
print(f"\n(для сравнения, эпизод №1 апр-сен 2026: среднее 8.75, медиана 7, мин/макс 1/24, 4 из 16 циклов <5 кандидатов)")

screens = {cp: bt.screen_at_date(cp, fundamentals, frames)[:bt.N_SLOTS] for cp in checkpoints}

def run_combo(sl_pct, tp_pct):
    slot_value = [bt.SLOT_USD] * bt.N_SLOTS
    trades = []
    for cp in checkpoints:
        chosen = screens[cp]
        for i in range(bt.N_SLOTS):
            if i >= len(chosen): continue
            sym, rank, price = chosen[i]
            bt.SL_PCT, bt.TP_PCT = sl_pct, tp_pct
            exit_price, exit_date, reason = bt.simulate_slot(sym, cp, price, frames)
            gross_pct = (exit_price - price) / price * 100.0
            net_pct = gross_pct - bt.COMMISSION_RT_PCT
            slot_value[i] *= (1 + net_pct / 100.0)
            trades.append({'net_pct': net_pct, 'reason': reason})
    total_end = sum(slot_value)
    reasons = {}
    for t in trades: reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    nets = [t['net_pct'] for t in trades]
    return {'return_pct': (total_end/(bt.SLOT_USD*bt.N_SLOTS)-1)*100, 'n': len(trades), 'reasons': reasons,
            'winrate': sum(1 for n in nets if n>0)/len(nets)*100 if nets else 0}

print(f"\n=== 'Лучший бычий' набор (SL=-10/TP=+10) на 01.02-21.04.2025 ===")
r = run_combo(10.0, 10.0)
print(f"Доходность: {r['return_pct']:+.2f}%  n={r['n']}  winrate={r['winrate']:.1f}%  {r['reasons']}")

print(f"\n=== 'Лучший медвежий' узкий набор (SL=1.5/TP=3) для сравнения ===")
r2 = run_combo(1.5, 3.0)
print(f"Доходность: {r2['return_pct']:+.2f}%  n={r2['n']}  winrate={r2['winrate']:.1f}%  {r2['reasons']}")

vti = yf.Ticker('VTI').history(start='2025-02-01', end='2025-04-22')
vti_ret = (vti['Close'].iloc[-1]/vti['Close'].iloc[0]-1)*100
print(f"\nVTI buy-and-hold за тот же период: {vti_ret:+.2f}%")
