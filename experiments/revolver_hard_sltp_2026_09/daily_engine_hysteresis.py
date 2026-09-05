"""
То же самое, что daily_engine_long_run.py, но с фильтром от ложных
переключений (hysteresis): реальную смену режима засчитываем только после
CONFIRM_DAYS подряд дней с новым знаком сигнала -- не в момент первого же
пересечения нуля. Прямой ответ на живую находку -- 6-дневный шумовой
"медвежий" всплеск 20-26.11.2025 внутри длинного бычьего периода, который ел
доходность переключения без всякой пользы. Claude/BACKLOG.md #167.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}

with open(CACHE_DIR / 'signal_frames_long_2025_2026.pkl', 'rb') as f:
    frames = pickle.load(f)

market = yf.Ticker('VTI').history(start='2024-11-01', end='2026-09-06', interval='1d')
market.index = market.index.tz_localize(None)
close = market['Close']
ema20 = close.ewm(span=20, adjust=False).mean()
ema50 = close.ewm(span=50, adjust=False).mean()
K, LAG = 7, 5
slope = (ema20 - ema20.shift(K)) / K
proj_ema20 = ema20 + LAG * slope
spread = (proj_ema20 - ema50) / ema50 * 100
raw_bullish = (spread > 0)

def build_confirmed_regime(confirm_days):
    """Смена подтверждённого режима засчитывается только после confirm_days
    подряд торговых дней с новым знаком сырого сигнала."""
    confirmed = pd.Series(index=raw_bullish.index, dtype=bool)
    current = raw_bullish.iloc[0]
    streak = 0
    last_raw = None
    for i, (ts, raw) in enumerate(raw_bullish.items()):
        if raw == last_raw:
            streak += 1
        else:
            streak = 1
        last_raw = raw
        if raw != current and streak >= confirm_days:
            current = raw
        confirmed.iloc[i] = current
    return confirmed

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}
bt.TIMEOUT_DAYS = 10
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
trading_days = [d.date() for d in close.index if START_DATE <= d.date() <= END_DATE]

class Slot:
    def __init__(self): self.symbol=None; self.value=SLOT_USD

def run(confirmed_regime):
    def regime_bullish(check_date):
        ts = pd.Timestamp(check_date)
        idx = confirmed_regime.index[confirmed_regime.index <= ts]
        if len(idx) == 0: return True
        return bool(confirmed_regime.loc[idx[-1]])

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

print(f"{'CONFIRM_DAYS':>14} {'Доходность':>12} {'Сделок':>7} {'Winrate':>8}  Даты смены режима")
for confirm_days in [1, 3, 5, 7, 10, 15]:
    confirmed = build_confirmed_regime(confirm_days)
    r = run(confirmed)
    print(f"{confirm_days:14} {r['return_pct']:+11.2f}% {r['n']:7} {r['winrate']:7.1f}%  {r['regime_log']}")

print(f"\n(для сравнения: фикс.бычий весь период +58.40%, фикс.медвежий +30.89%, VTI +30.45%)")
