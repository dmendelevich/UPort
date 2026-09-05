"""
Измеряет реальное отставание (лаг) EMA20/EMA50 и "спроецированной" (lead-
compensated) версии от истинных разворотов цены VTI -- проверка эмпирической
гипотезы пользователя ("на глаз EMA50 отстаёт дней на 7") и калибровка сигнала
режима для daily_engine.py. Сессия 2026-09-05, Claude/BACKLOG.md #167.

Метод: находим реальные локальные минимумы цены (окно ±10 торговых дней), для
каждого ищем ближайший следующий локальный минимум сравниваемого ряда (EMA20,
EMA50, или спроецированная версия), лаг = разница дат в днях.

"Спроецированная" версия -- lead-compensated EMA (тот же приём, что у Hull MA/
Zero-Lag EMA): projected(t) = EMA20(t) + LAG_DAYS x наклон_за_K_дней(t).
"""
import pandas as pd
import numpy as np
import yfinance as yf

WINDOW = 10  # дней в обе стороны для признания точки локальным экстремумом цены


def find_swing_lows(series, window=WINDOW):
    lows = []
    vals = series.values
    for i in range(window, len(vals) - window):
        seg = vals[i - window:i + window + 1]
        if vals[i] == seg.min():
            lows.append(i)
    merged = []
    for i in lows:
        if merged and i - merged[-1] < window:
            if vals[i] < vals[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    return merged


def find_local_min_idx(series, start_idx, search_ahead=40):
    vals = series.values
    end = min(len(vals) - 1, start_idx + search_ahead)
    for i in range(start_idx + 1, end):
        if vals[i] <= vals[i - 1] and vals[i] <= vals[i + 1] and vals[i] < vals[i - 3] and (i + 3 >= len(vals) or vals[i] < vals[i + 3]):
            return i
    return None


def measure_lag(price_series, compare_series, price_low_idx, label):
    lags = []
    for idx in price_low_idx:
        date_price = price_series.index[idx]
        min_idx = find_local_min_idx(compare_series, idx, search_ahead=30)
        if min_idx:
            lags.append((compare_series.index[min_idx] - date_price).days)
    s = pd.Series(lags)
    print(f"{label:35} n={len(s):3}  среднее={s.mean():6.2f}  медиана={s.median():5.1f}  std={s.std():6.2f}  диапазон=[{s.min()},{s.max()}]")
    return s


def run():
    h = yf.Ticker('VTI').history(start='2021-01-01', end='2026-09-06', interval='1d')
    h.index = h.index.tz_localize(None)
    close = h['Close']
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    price_low_idx = find_swing_lows(close)
    print(f"Найдено разворотных минимумов цены (окно ±{WINDOW} дн.): {len(price_low_idx)}\n")

    measure_lag(close, ema20, price_low_idx, "EMA20 как есть")
    measure_lag(close, ema50, price_low_idx, "EMA50 как есть")

    print("\n--- Спроецированная (lead-compensated) EMA20, сетка K/LAG ---")
    best = None
    for K in [5, 7, 10, 14]:
        slope = (ema20 - ema20.shift(K)) / K
        for LAG in [5, 7, 10]:
            projected = ema20 + LAG * slope
            s = measure_lag(close, projected, price_low_idx, f"projected K={K:2} LAG={LAG:2}")
            if best is None or s.mean() < best[0]:
                best = (s.mean(), K, LAG)
    print(f"\nЛучшая по среднему лагу комбинация: K={best[1]}, LAG={best[2]} (среднее {best[0]:.2f} дн.)")


if __name__ == '__main__':
    run()
