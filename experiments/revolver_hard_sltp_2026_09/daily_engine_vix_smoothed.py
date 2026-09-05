"""
VIX, сглаженный коротким EMA (3/5 дней -- не 20, как для цены: лаг у VIX и так
почти нулевой, сглаживание нужно только против дневного шума, не против
структурного отставания), + порог. Продолжение daily_engine_vix.py после
провала сырого порога (26-47 ложных переключений). Claude/BACKLOG.md #167.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
with open(CACHE_DIR / 'signal_frames_long_2025_2026.pkl', 'rb') as f:
    frames = pickle.load(f)

vix = yf.Ticker('^VIX').history(start='2024-11-01', end='2026-09-06', interval='1d')
vix.index = vix.index.tz_localize(None)
vix_close = vix['Close']

market = yf.Ticker('VTI').history(start='2024-11-01', end='2026-09-06', interval='1d')
market.index = market.index.tz_localize(None)
trading_days_full = market.index

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}
bt.TIMEOUT_DAYS = 10
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
trading_days = [d.date() for d in trading_days_full if START_DATE <= d.date() <= END_DATE]

class Slot:
    def __init__(self): self.symbol=None; self.value=SLOT_USD

def make_regime_fn(vix_series, threshold):
    def regime_bullish(check_date):
        ts = pd.Timestamp(check_date)
        idx = vix_series.index[vix_series.index <= ts]
        if len(idx) == 0: return True
        return float(vix_series.loc[idx[-1]]) <= threshold
    return regime_bullish

def run(regime_fn):
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
            bullish = regime_fn(day)
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

print(f"{'EMA-окно':>10} {'Порог':>7} {'Доходность':>12} {'Сделок':>7} {'Winrate':>8} {'Смен':>5}")
for span in [3, 5, 10]:
    vix_smooth = vix_close.ewm(span=span, adjust=False).mean()
    for thr in [19.0, 20.0, 21.0]:
        r = run(make_regime_fn(vix_smooth, thr))
        print(f"{span:10} {thr:7.1f} {r['return_pct']:+11.2f}% {r['n']:7} {r['winrate']:7.1f}% {len(r['regime_log']):5}")

print(f"\n(для сравнения: фикс.бычий +58.40%, EMA-переключение +57.38%, лучший сырой VIX (порог=21) +49.17%)")
