"""
Проверка находки №2 на ВТОРОМ, НЕЗАВИСИМОМ периоде (2026-09-06) -- та же логика,
что diversify_horizons_check.py, но на 2022 годе (отдельный кэш, не пересекается
с 2025-02..2026-09) -- урок сессии про переподгонку на одном окне (BACKLOG,
"фундаментал-26 к 23-му"): не доверять находке без независимой проверки.

2022 -- заведомо ДРУГОЙ характер рынка (настоящий медвежий год + частичное
восстановление), а не продолжение того же бычьего окна -- честная проверка,
не то же самое окно другими словами.
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

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}
bt.TIMEOUT_DAYS = 10
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2022, 1, 3), date(2022, 10, 15)
market_index = next(iter(frames.values())).index
trading_days = [d.date() for d in market_index if START_DATE <= d.date() <= END_DATE]


class Slot:
    def __init__(self, params):
        self.symbol = None
        self.value = SLOT_USD
        self.params = params


def run(n_bull, n_bear):
    slots = [Slot(BULL) for _ in range(n_bull)] + [Slot(BEAR) for _ in range(n_bear)]
    trades_log = []
    screen_cache = {}
    daily_portfolio_value = []

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
            low, high = float(row['low']), float(row['high'])
            days_held = (day - s.entry_date).days
            exit_price, reason = None, None
            if low <= s.sl_price:
                exit_price, reason = s.sl_price, 'SL'
            elif high >= s.tp_price:
                exit_price, reason = s.tp_price, 'TP'
            elif days_held >= bt.TIMEOUT_DAYS:
                exit_price, reason = float(row['close']), 'TIMEOUT'
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
                s.symbol, s.entry_date, s.entry_price = sym, day, price
                s.sl_price = price * (1 - s.params['sl'] / 100.0)
                s.tp_price = price * (1 + s.params['tp'] / 100.0)
                held.add(sym)

        mtm_total = 0.0
        for s in slots:
            if s.symbol is None:
                mtm_total += s.value
                continue
            df = frames[s.symbol]
            if ts in df.index:
                current_close = float(df.loc[ts]['close'])
                mtm_total += s.value * (current_close / s.entry_price)
            else:
                mtm_total += s.value
        daily_portfolio_value.append(mtm_total)

    total_end = sum(s.value for s in slots)
    values = pd.Series(daily_portfolio_value)
    max_dd = ((values - values.cummax()) / values.cummax() * 100.0).min()
    return total_end, trades_log, max_dd


if __name__ == '__main__':
    print(f"Период: {START_DATE} -> {END_DATE} (2022, независимый от 2025-2026)")
    print(f"{'Вариант':>28} {'Доходность':>12} {'Сделок':>7} {'Просадка':>10} {'Дох/Просадка':>13}")
    for n_bull, n_bear, label in [(10, 0, '100% широкий'), (0, 10, '100% узкий'), (5, 5, 'Диверсификация 5/5'), (7, 3, 'Диверсификация 7/3'), (3, 7, 'Диверсификация 3/7')]:
        total_end, trades, max_dd = run(n_bull, n_bear)
        ret = (total_end / (SLOT_USD * N_SLOTS) - 1) * 100
        ratio = ret / abs(max_dd) if max_dd != 0 else float('nan')
        print(f"{label:>28} {ret:+11.2f}% {len(trades):7} {max_dd:+9.2f}% {ratio:13.2f}")

    import yfinance as yf
    vti = yf.Ticker('VTI').history(start='2022-01-03', end='2022-10-16')
    vti_ret = (vti['Close'].iloc[-1] / vti['Close'].iloc[0] - 1) * 100
    print(f"\nVTI buy-and-hold 2022: {vti_ret:+.2f}%")
