"""
Замер лага VIX относительно реальных разворотов цены VTI -- та же методология
и те же 42 разворота, что и в regime_lag_analysis.py (EMA20/EMA50), только
источник другой (подразумеваемая волатильность опционного рынка, не
сглаживание той же цены). Claude/BACKLOG.md #167, продолжение "детектор
режима" по просьбе пользователя.

VIX движется ОБРАТНО цене -- пик страха (локальный максимум VIX) обычно
совпадает с или предшествует дну цены. Ищем ближайший по времени локальный
максимум VIX к каждому реальному минимуму цены -- лаг может быть
ОТРИЦАТЕЛЬНЫМ (VIX опередил дно), в отличие от EMA, где лаг был только
положительным.
"""
import pandas as pd
import numpy as np
import yfinance as yf

PRICE_WINDOW = 10
VIX_WINDOW = 5
SEARCH_RANGE = 25  # дней в обе стороны для поиска ближайшего пика VIX


def find_swing_extrema(series, window, find_max=False):
    vals = series.values
    idxs = []
    for i in range(window, len(vals) - window):
        seg = vals[i - window:i + window + 1]
        target = seg.max() if find_max else seg.min()
        if vals[i] == target:
            idxs.append(i)
    merged = []
    for i in idxs:
        if merged and i - merged[-1] < window:
            better = (vals[i] > vals[merged[-1]]) if find_max else (vals[i] < vals[merged[-1]])
            if better:
                merged[-1] = i
        else:
            merged.append(i)
    return merged


h = yf.Ticker('VTI').history(start='2021-01-01', end='2026-09-06', interval='1d')
h.index = h.index.tz_localize(None)
close = h['Close']
price_low_idx = find_swing_extrema(close, PRICE_WINDOW, find_max=False)
price_low_dates = [close.index[i] for i in price_low_idx]
print(f"Найдено разворотных минимумов цены: {len(price_low_idx)}")

vix = yf.Ticker('^VIX').history(start='2021-01-01', end='2026-09-06', interval='1d')
vix.index = vix.index.tz_localize(None)
vix_close = vix['Close']
vix_peak_idx = find_swing_extrema(vix_close, VIX_WINDOW, find_max=True)
vix_peak_dates = [vix_close.index[i] for i in vix_peak_idx]
print(f"Найдено пиков VIX (окно ±{VIX_WINDOW} дн.): {len(vix_peak_dates)}")

lags = []
for pd_date in price_low_dates:
    candidates = [(abs((vp - pd_date).days), (vp - pd_date).days) for vp in vix_peak_dates
                  if abs((vp - pd_date).days) <= SEARCH_RANGE]
    if not candidates:
        continue
    candidates.sort()
    lags.append(candidates[0][1])  # знаковый лаг ближайшего пика

s = pd.Series(lags)
print(f"\nСовпало с разворотом (в пределах ±{SEARCH_RANGE} дн.): {len(s)} из {len(price_low_dates)}")
print(f"Лаг VIX относительно дна цены (отрицательный = VIX опередил):")
print(s.describe(percentiles=[.1, .25, .5, .75, .9]))
print(f"\nОпередил (лаг<0): {(s < 0).sum()}  Совпал день-в-день (лаг=0): {(s == 0).sum()}  Отстал (лаг>0): {(s > 0).sum()}")

print(f"\n--- Для сравнения (regime_lag_analysis.py) ---")
print("EMA20 как есть: среднее 11.63, медиана 7.5 (всегда положительный -- отставание)")
print("EMA50 как есть: среднее 14.08, медиана 9.0")
print("Спроецированная EMA20 (лучшая, K=7/LAG=5): среднее 4.76, медиана 4.0")
