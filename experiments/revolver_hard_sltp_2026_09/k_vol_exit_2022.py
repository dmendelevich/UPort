"""
K_vol_exit_long_run.py, но на независимом 2022 периоде -- завершает проверку
"бьёт ли РЕАЛЬНОЕ правило Револьверной (не плоский SL/TP) VTI при честной
комиссии" на медвежьем годе, не только на 2025-2026.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date
import numpy as np
import pandas as pd
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
with open(CACHE_DIR / 'signal_frames_2022_full.pkl', 'rb') as f:
    frames = pickle.load(f)

K_STOP, K_TRAIL = 6.0, 5.0
TARGET_PCT = 6.0
CONFIRM_DAYS_EXIT = 12
TIME_LIMIT_DAYS = 30
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2022, 1, 3), date(2022, 10, 15)
market_index = next(iter(frames.values())).index
trading_days = [d.date() for d in market_index if START_DATE <= d.date() <= END_DATE]


class Slot:
    def __init__(self):
        self.symbol = None
        self.value = SLOT_USD


def run():
    slots = [Slot() for _ in range(N_SLOTS)]
    trades_log = []
    screen_cache = {}

    def get_screen(d):
        if d not in screen_cache:
            screen_cache[d] = bt.screen_at_date(d, fundamentals, frames)
        return screen_cache[d]

    for day in trading_days:
        ts = pd.Timestamp(day)
        for s in slots:
            if s.symbol is None:
                continue
            df = frames[s.symbol]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            low, high, close = float(row['low']), float(row['high']), float(row['close'])
            vol = float(row['vol_pct']) if not np.isnan(row['vol_pct']) else 0.0
            streak = row['streak']
            days_held = (day - s.entry_date).days
            s.peak = max(s.peak, high)
            exit_price, reason = None, None

            if vol > 0:
                sl_price = s.entry_price * (1 - K_STOP * vol / 100.0)
                if low <= sl_price:
                    exit_price, reason = sl_price, 'SL'
                elif s.peak > s.entry_price * 1.005:
                    trail_price = s.peak * (1 - K_TRAIL * vol / 100.0)
                    if low <= trail_price:
                        exit_price, reason = trail_price, 'TS'

            if exit_price is None:
                profit_pct = (close - s.entry_price) / s.entry_price * 100.0
                if profit_pct >= TARGET_PCT:
                    exit_price, reason = close, 'TP'
                elif streak is not None and not np.isnan(streak) and streak <= -CONFIRM_DAYS_EXIT:
                    exit_price, reason = close, 'BREAK'
                elif days_held >= TIME_LIMIT_DAYS:
                    exit_price, reason = close, 'TIMEOUT'

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
            for s in empty_slots:
                if not candidates:
                    break
                sym, rank, price = candidates.pop(0)
                s.symbol, s.entry_date, s.entry_price, s.peak = sym, day, price, price
                held.add(sym)

    total_end = sum(s.value for s in slots)
    df = pd.DataFrame(trades_log)
    print(f"K_stop={K_STOP} K_trail={K_TRAIL} target=+{TARGET_PCT}% confirm={CONFIRM_DAYS_EXIT}d timeout={TIME_LIMIT_DAYS}d")
    print(f"Период: {START_DATE} -> {END_DATE} (2022)")
    print(f"Старт ${SLOT_USD*N_SLOTS:,.2f} -> Финиш ${total_end:,.2f}  ({(total_end/(SLOT_USD*N_SLOTS)-1)*100:+.2f}%)")
    print(f"Сделок: {len(df)}, winrate: {(df['net_pct']>0).mean()*100:.1f}%\n")
    for reason, g in df.groupby('reason'):
        print(f"  {reason:9} n={len(g):3} ({len(g)/len(df)*100:4.1f}%)  net_среднее={g['net_pct'].mean():+6.2f}%")
    return df


if __name__ == '__main__':
    run()
    import yfinance as yf
    vti = yf.Ticker('VTI').history(start='2022-01-03', end='2022-10-16')
    vti_ret = (vti['Close'].iloc[-1] / vti['Close'].iloc[0] - 1) * 100
    print(f"\nVTI buy-and-hold 2022: {vti_ret:+.2f}%")
