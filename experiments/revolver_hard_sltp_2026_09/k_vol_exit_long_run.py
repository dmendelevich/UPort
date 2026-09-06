"""
Продолжение темы (Claude/BACKLOG.md, "мировая практика" -- находка №1, 2026-09-06):
пересчёт того же 19-месячного прогона (daily_engine_long_run.py), но с РЕАЛЬНЫМ
выходом Револьверной вместо плоского SL/TP -- K x волатильность (stop-loss K=6,
trailing K=5, обе от capital_protection_watcher.py) + цель +6% / подтверждённый
слом streak<=-12 / тайм-аут 30д (position_exit_evaluator.py::_check_revolver_exit).

Упрощение (то же, что и в остальной серии): "цель достигнута, момент жив ->
перенос в Трендовую" не моделируется -- любое достижение цели или тайм-аута
считается чистым выходом слота, как и в daily_engine*.py. Приоритет проверок
внутри дня -- как в реальной системе (capital_protection_watcher быстрее и
проверяется раньше стратегийных сигналов): SL -> трейлинг -> цель -> слом -> тайм-аут.

Слот/капитал -- та же структура $1000 x 10, что и в daily_engine_long_run.py,
чтобы сравнение "сколько теряем на SL относительно всей прибыли" было
яблоки-к-яблокам, меняем только правило выхода, не структуру капитала.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import numpy as np
import pandas as pd
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}

with open(CACHE_DIR / 'signal_frames_long_2025_2026.pkl', 'rb') as f:
    frames = pickle.load(f)
print(f"Кэш найден: {len(frames)} тикеров")

K_STOP, K_TRAIL = 6.0, 5.0
TARGET_PCT = 6.0
CONFIRM_DAYS_EXIT = 12
TIME_LIMIT_DAYS = 30
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
market_index = None
for sym, df in frames.items():
    market_index = df.index
    break
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
                trades_log.append({'reason': reason, 'net_pct': net_pct, 'symbol': s.symbol})
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
    print(f"Старт ${SLOT_USD*N_SLOTS:,.2f} -> Финиш ${total_end:,.2f}  ({(total_end/(SLOT_USD*N_SLOTS)-1)*100:+.2f}%)")
    print(f"Сделок: {len(df)}, winrate: {(df['net_pct']>0).mean()*100:.1f}%\n")
    for reason, g in df.groupby('reason'):
        print(f"  {reason:9} n={len(g):3} ({len(g)/len(df)*100:4.1f}%)  net: мин={g['net_pct'].min():+6.2f}% "
              f"макс={g['net_pct'].max():+6.2f}% среднее={g['net_pct'].mean():+6.2f}%  сумма_net%={g['net_pct'].sum():+7.2f}")
    return df


if __name__ == '__main__':
    run()
