"""
Продолжение k_vol_exit_long_run.py -- проверка эффекта гибридного sizing
(тема №1 из "мировой практики", 2026-09-06): слот остаётся потолком ($1000),
но вниз урезается по волатильности, если K_stop x vol превышает потолок риска:

    qty_factor = min(1, RISK_CEILING_PCT / (K_STOP * vol_at_entry))
    invested = SLOT_USD * qty_factor, остаток слота -- простаивающий кэш (0% доходности)
    итог: s.value *= (1 + qty_factor * net_pct / 100)

Прогоняется 3 варианта на ТОМ ЖЕ 19-месячном периоде/входе/правиле выхода,
что и k_vol_exit_long_run.py -- разница ТОЛЬКО в RISK_CEILING_PCT (None = без
урезания, нынешнее поведение = baseline для сравнения).
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

K_STOP, K_TRAIL = 6.0, 5.0
TARGET_PCT = 6.0
CONFIRM_DAYS_EXIT = 12
TIME_LIMIT_DAYS = 30
N_SLOTS, SLOT_USD = bt.N_SLOTS, bt.SLOT_USD

START_DATE, END_DATE = date(2025, 2, 1), date(2026, 9, 5)
market_index = next(iter(frames.values())).index
trading_days = [d.date() for d in market_index if START_DATE <= d.date() <= END_DATE]


class Slot:
    def __init__(self):
        self.symbol = None
        self.value = SLOT_USD


def run(risk_ceiling_pct):
    slots = [Slot() for _ in range(N_SLOTS)]
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
                s.value *= (1 + s.qty_factor * net_pct / 100.0)
                trades_log.append({'reason': reason, 'net_pct': net_pct, 'qty_factor': s.qty_factor})
                s.symbol = None

        empty_slots = [s for s in slots if s.symbol is None]
        if empty_slots:
            held = {s.symbol for s in slots if s.symbol}
            candidates = [c for c in get_screen(day) if c[0] not in held]
            for s in empty_slots:
                if not candidates:
                    break
                sym, rank, price = candidates.pop(0)
                entry_vol = frames[sym]['vol_pct'].get(ts, np.nan)
                entry_vol = float(entry_vol) if not (entry_vol is None or np.isnan(entry_vol)) else 0.0
                if risk_ceiling_pct is None or entry_vol <= 0:
                    qty_factor = 1.0
                else:
                    qty_factor = min(1.0, risk_ceiling_pct / (K_STOP * entry_vol))
                s.symbol, s.entry_date, s.entry_price, s.peak, s.qty_factor = sym, day, price, price, qty_factor
                held.add(sym)

        # Дневная переоценка портфеля (mark-to-market) для просадки -- s.value для
        # ОТКРЫТОЙ позиции остаётся "стоимостью на момент входа" (обновляется только
        # при закрытии), реальная текущая стоимость = простаивающая часть слота
        # (1-qty_factor) + инвестированная часть, переоценённая по текущей цене.
        mtm_total = 0.0
        for s in slots:
            if s.symbol is None:
                mtm_total += s.value
                continue
            df = frames[s.symbol]
            if ts in df.index:
                current_close = float(df.loc[ts]['close'])
                mtm_total += s.value * (1 + s.qty_factor * (current_close / s.entry_price - 1))
            else:
                mtm_total += s.value
        daily_portfolio_value.append(mtm_total)

    total_end = sum(s.value for s in slots)
    values = pd.Series(daily_portfolio_value)
    running_max = values.cummax()
    drawdown_pct = (values - running_max) / running_max * 100.0
    max_dd = drawdown_pct.min()
    df = pd.DataFrame(trades_log)
    label = f"потолок={risk_ceiling_pct}%" if risk_ceiling_pct else "БЕЗ урезания (baseline)"
    print(f"\n=== {label} ===")
    print(f"Финиш ${total_end:,.2f}  ({(total_end/(SLOT_USD*N_SLOTS)-1)*100:+.2f}%)  сделок={len(df)}  "
          f"winrate={(df['net_pct']>0).mean()*100:.1f}%  средний_qty_factor={df['qty_factor'].mean():.2f}  "
          f"макс_просадка={max_dd:+.2f}%")
    for reason, g in df.groupby('reason'):
        print(f"  {reason:9} n={len(g):3} ({len(g)/len(df)*100:4.1f}%)  net_среднее={g['net_pct'].mean():+6.2f}%  "
              f"qty_factor_среднее={g['qty_factor'].mean():.2f}")
    return total_end


if __name__ == '__main__':
    for ceiling in [None, 30.0, 25.0, 15.0, 10.0]:
        run(ceiling)
