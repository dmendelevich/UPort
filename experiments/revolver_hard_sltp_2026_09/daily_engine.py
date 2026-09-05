"""
Ежедневный движок: слоты дозаполняются непрерывно (не пачкой раз в 10 дней), а
режим (бычий/медвежий по спроецированной EMA20 vs EMA50) проверяется тоже
ежедневно -- развязка частоты проверки режима от длины цикла удержания позиции.
Продолжение backtest.py, сессия 2026-09-05, Claude/BACKLOG.md #167.

Использует тот же screen_at_date/simulate-логику полей, что и backtest.py --
меняется только оболочка: вместо "раз в RESCAN_DAYS пересобрать все N_SLOTS
разом" -- "каждый торговый день проверить каждую открытую позицию на SL/TP/
тайм-аут, и точечно дозаполнить только реально освободившиеся слоты".

Запуск: python3 daily_engine.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt
import pandas as pd
import yfinance as yf

START_DATE = date(2026, 2, 1)
END_DATE = date(2026, 9, 5)

BULL = {'sl': 10.0, 'tp': 10.0}
BEAR = {'sl': 1.5, 'tp': 3.0}

# Лид-компенсированный сигнал режима -- см. regime_lag_analysis.py (K=7, LAG=5
# минимизирует средний лаг относительно реальных разворотов цены VTI).
K, LAG = 7, 5


def build_regime_signal():
    market = yf.Ticker('VTI').history(start='2025-10-01', end='2026-09-06', interval='1d')
    market.index = market.index.tz_localize(None)
    close = market['Close']
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    slope = (ema20 - ema20.shift(K)) / K
    proj_ema20 = ema20 + LAG * slope
    spread = (proj_ema20 - ema50) / ema50 * 100
    return spread, close


def regime_bullish(spread_series, check_date):
    ts = pd.Timestamp(check_date)
    idx = spread_series.index[spread_series.index <= ts]
    if len(idx) == 0:
        return True
    return float(spread_series.loc[idx[-1]]) > 0


class Slot:
    def __init__(self):
        self.symbol = None
        self.value = bt.SLOT_USD


def run(mode, spread_series, trading_days, fundamentals, frames):
    """mode: 'bull_fixed' | 'bear_fixed' | 'switch'"""
    slots = [Slot() for _ in range(bt.N_SLOTS)]
    trades_log = []
    screen_cache = {}

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
                trades_log.append({'reason': reason, 'net_pct': net_pct})
                s.symbol = None

        empty_slots = [s for s in slots if s.symbol is None]
        if empty_slots:
            held = {s.symbol for s in slots if s.symbol}
            candidates = [c for c in get_screen(day) if c[0] not in held]
            if mode == 'bull_fixed':
                params = BULL
            elif mode == 'bear_fixed':
                params = BEAR
            else:
                params = BULL if regime_bullish(spread_series, day) else BEAR
            for s in empty_slots:
                if not candidates:
                    break
                sym, rank, price = candidates.pop(0)
                s.symbol, s.entry_date, s.entry_price = sym, day, price
                s.sl_price = price * (1 - params['sl'] / 100.0)
                s.tp_price = price * (1 + params['tp'] / 100.0)
                held.add(sym)

    total_end = sum(s.value for s in slots)
    nets = [t['net_pct'] for t in trades_log]
    reasons = {}
    for t in trades_log:
        reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    return {
        'return_pct': (total_end / (bt.SLOT_USD * bt.N_SLOTS) - 1) * 100,
        'n': len(trades_log), 'reasons': reasons,
        'winrate': sum(1 for n in nets if n > 0) / len(nets) * 100 if nets else 0,
    }


def main():
    spread, market_close = build_regime_signal()
    trading_days = [d.date() for d in market_close.index if START_DATE <= d.date() <= END_DATE]
    fundamentals, frames = bt.load_universe_and_history()

    print(f"{'Вариант (ежедневный движок)':>32} {'Доходность':>12} {'Сделок':>7} {'Winrate':>8}")
    for mode, label in [
        ('bull_fixed', 'Фикс. бычий, весь период'),
        ('bear_fixed', 'Фикс. медвежий, весь период'),
        ('switch', 'Переключение (спроецированный)'),
    ]:
        r = run(mode, spread, trading_days, fundamentals, frames)
        print(f"{label:>32} {r['return_pct']:+11.2f}% {r['n']:7} {r['winrate']:7.1f}%  {r['reasons']}")


if __name__ == '__main__':
    main()
