"""
"Мировая практика" находка №2 (2026-09-06): вместо детектора режима (8 попыток,
тема #168, ни одна не обогнала константу) -- держать ОБА набора параметров
(широкий/узкий из #167) одновременно, на раздельных долях слотов, без всякого
детектора -- та же логика, что у CTA-практики "смешивать горизонты тренда"
(Baltas & Kosowski 2013, Hurst et al. 2017 -- быстрый и медленный сигнал вместе
эффективнее, чем попытка угадать, какой сейчас нужен).

Тот же 19-месячный период/вход/данные, что и daily_engine_long_run.py -- разница
только в РЕЖИМЕ: 5 слотов всегда на широких параметрах (BULL), 5 слотов всегда
на узких (BEAR), общий пул кандидатов (не покупают одну бумагу дважды).
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
with open(CACHE_DIR / 'signal_frames_long_2025_2026.pkl', 'rb') as f:
    frames = pickle.load(f)
symbols = list(fundamentals.keys())

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}
bt.TIMEOUT_DAYS = 10
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
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
        for s in slots:
            if s.symbol is None:
                continue
            df = frames[s.symbol]
            ts = pd.Timestamp(day)
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
                trades_log.append({'reason': reason, 'net_pct': net_pct, 'group': 'BULL' if s.params is BULL else 'BEAR'})
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

        # Дневная переоценка для просадки -- s.value открытой позиции остаётся
        # "стоимостью на момент входа" (обновляется только при закрытии), текущая
        # честная стоимость = переоценка по сегодняшнему close.
        ts = pd.Timestamp(day)
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
    print(f"{'Вариант':>28} {'Доходность':>12} {'Сделок':>7} {'Просадка':>10} {'Дох/Просадка':>13}")
    for n_bull, n_bear, label in [(10, 0, '100% широкий (baseline)'), (0, 10, '100% узкий (baseline)'), (5, 5, 'Диверсификация 5/5'), (7, 3, 'Диверсификация 7/3'), (3, 7, 'Диверсификация 3/7')]:
        total_end, trades, max_dd = run(n_bull, n_bear)
        ret = (total_end / (SLOT_USD * N_SLOTS) - 1) * 100
        print(f"{label:>28} {ret:+11.2f}% {len(trades):7} {max_dd:+9.2f}% {ret/abs(max_dd):13.2f}")

    vti = None
    import yfinance as yf
    vti = yf.Ticker('VTI').history(start='2025-02-01', end='2026-09-06')
    vti_ret = (vti['Close'].iloc[-1] / vti['Close'].iloc[0] - 1) * 100
    print(f"\nVTI buy-and-hold: {vti_ret:+.2f}%")
    print("Для справки (тема #168, тот же период): фикс.бычий весь период +58.40%, фикс.медвежий +30.89%, переключение (спроецированный EMA) +57.38%")
