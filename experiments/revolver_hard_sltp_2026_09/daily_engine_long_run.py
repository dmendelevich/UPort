"""
Пересчёт переключения на ежедневном движке (daily_engine.py) на ДЛИННОМ
непрерывном отрезке 01.02.2025-05.09.2026 (~19 месяцев), охватывающем все 4
найденных в серии сегмента режима подряд: медвежий фев-апр'25 -> бычий
апр'25-фев'26 -> медвежий фев-мар'26 -> бычий апр-сен'26. Раньше переключение
проверялось только на одном переходе медведь->бык (Claude/BACKLOG.md #167,
01.02-05.09.2026) -- этот прогон честнее: несколько независимых переключений
подряд, не один.
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

CACHE = str(CACHE_DIR / 'signal_frames_long_2025_2026.pkl')
try:
    with open(CACHE, 'rb') as f:
        frames = pickle.load(f)
    print(f"Кэш найден: {len(frames)} тикеров")
except FileNotFoundError:
    print("Скачиваю историю цен за ноябрь 2024 - сентябрь 2026 (это займёт время)...")
    raw = yf.download(symbols, start='2024-11-01', end='2026-09-06', interval='1d',
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

# --- сигнал режима: спроецированная EMA20 vs EMA50 (K=7, LAG=5) ---
market = yf.Ticker('VTI').history(start='2024-11-01', end='2026-09-06', interval='1d')
market.index = market.index.tz_localize(None)
close = market['Close']
ema20 = close.ewm(span=20, adjust=False).mean()
ema50 = close.ewm(span=50, adjust=False).mean()
K, LAG = 7, 5
slope = (ema20 - ema20.shift(K)) / K
proj_ema20 = ema20 + LAG * slope
spread = (proj_ema20 - ema50) / ema50 * 100

def regime_bullish(check_date):
    ts = pd.Timestamp(check_date)
    idx = spread.index[spread.index <= ts]
    if len(idx) == 0: return True
    return float(spread.loc[idx[-1]]) > 0

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}
bt.TIMEOUT_DAYS = 10
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
trading_days = [d.date() for d in close.index if START_DATE <= d.date() <= END_DATE]

class Slot:
    def __init__(self): self.symbol=None; self.value=SLOT_USD

def run(mode):
    slots = [Slot() for _ in range(N_SLOTS)]
    trades_log = []
    screen_cache = {}
    regime_log = []
    def get_screen(d):
        if d not in screen_cache:
            screen_cache[d] = bt.screen_at_date(d, fundamentals, frames)
        return screen_cache[d]

    prev_regime = None
    for day in trading_days:
        for s in slots:
            if s.symbol is None: continue
            df = frames[s.symbol]; ts = pd.Timestamp(day)
            if ts not in df.index: continue
            row = df.loc[ts]
            low, high = float(row['low']), float(row['high'])
            days_held = (day - s.entry_date).days
            exit_price, reason = None, None
            if low <= s.sl_price: exit_price, reason = s.sl_price, 'SL'
            elif high >= s.tp_price: exit_price, reason = s.tp_price, 'TP'
            elif days_held >= bt.TIMEOUT_DAYS: exit_price, reason = float(row['close']), 'TIMEOUT'
            if exit_price is not None:
                gross_pct = (exit_price - s.entry_price) / s.entry_price * 100.0
                net_pct = gross_pct - bt.COMMISSION_RT_PCT
                s.value *= (1 + net_pct / 100.0)
                trades_log.append({'reason': reason, 'net_pct': net_pct})
                s.symbol = None
        empty_slots = [s for s in slots if s.symbol is None]
        if empty_slots:
            held = {s.symbol for s in slots if s.symbol}
            candidates = [c for c in get_screen(day) if c[0] not in held]
            if mode == 'bull_fixed': params = BULL
            elif mode == 'bear_fixed': params = BEAR
            else:
                bullish = regime_bullish(day)
                if prev_regime is not None and bullish != prev_regime:
                    regime_log.append((day, 'БЫЧИЙ' if bullish else 'МЕДВЕЖИЙ'))
                prev_regime = bullish
                params = BULL if bullish else BEAR
            for s in empty_slots:
                if not candidates: break
                sym, rank, price = candidates.pop(0)
                s.symbol, s.entry_date, s.entry_price = sym, day, price
                s.sl_price = price * (1 - params['sl']/100.0)
                s.tp_price = price * (1 + params['tp']/100.0)
                held.add(sym)
    total_end = sum(s.value for s in slots)
    nets = [t['net_pct'] for t in trades_log]
    reasons = {}
    for t in trades_log: reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    return {'return_pct': (total_end/(SLOT_USD*N_SLOTS)-1)*100, 'n': len(trades_log), 'reasons': reasons,
            'winrate': sum(1 for n in nets if n>0)/len(nets)*100 if nets else 0, 'regime_log': regime_log}

print(f"{'Вариант':>32} {'Доходность':>12} {'Сделок':>7} {'Winrate':>8}")
for mode, label in [('bull_fixed','Фикс. бычий, весь период'), ('bear_fixed','Фикс. медвежий, весь период'), ('switch','Переключение (спроецированный)')]:
    r = run(mode)
    print(f"{label:>32} {r['return_pct']:+11.2f}% {r['n']:7} {r['winrate']:7.1f}%  {r['reasons']}")
    if mode == 'switch':
        print("  Даты смены режима:", r['regime_log'])

vti = yf.Ticker('VTI').history(start='2025-02-01', end='2026-09-06')
vti_ret = (vti['Close'].iloc[-1]/vti['Close'].iloc[0]-1)*100
print(f"\nVTI buy-and-hold 01.02.2025-05.09.2026: {vti_ret:+.2f}%")
