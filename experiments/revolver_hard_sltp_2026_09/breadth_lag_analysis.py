"""
Проверка "ширины рынка" (breadth) как источника сигнала режима -- % бумаг
нашего же универса выше своей 50-дневной средней, агрегат по СОТНЯМ отдельных
бумаг, а не сглаживание одного индекса. Та же методология лага, что и для
EMA/VIX (regime_lag_analysis.py, vix_lag_analysis.py). Claude/BACKLOG.md #168
(продолжение), по просьбе пользователя.

Для замера лага достаточно представительной подвыборки (не всех 503 -- дорого
качать полную историю 2021-2026 по каждому), взято 100 тикеров.
"""
import warnings, pickle
warnings.filterwarnings('ignore')
from pathlib import Path
import json
import numpy as np
import pandas as pd
import yfinance as yf

CACHE_DIR = Path('/root/UPort/experiments/revolver_hard_sltp_2026_09/_cache')
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = json.load(f)
symbols = [r['symbol'] for r in fundamentals][:100]

BREADTH_CACHE = CACHE_DIR / 'breadth_2021_2026_100.pkl'
try:
    with open(BREADTH_CACHE, 'rb') as f:
        breadth = pickle.load(f)
    print("Кэш ширины рынка найден.")
except FileNotFoundError:
    print(f"Скачиваю историю {len(symbols)} тикеров за 2021-2026 для расчёта breadth...")
    raw = yf.download(symbols, start='2020-10-01', end='2026-09-06', interval='1d',
                       group_by='ticker', threads=True, progress=False, auto_adjust=False)
    above_ma_frames = []
    for sym in symbols:
        try:
            close = raw[sym]['Close'].dropna()
        except Exception:
            continue
        if len(close) < 60:
            continue
        ma50 = close.rolling(50).mean()
        above_ma_frames.append((close > ma50).rename(sym))
    combined = pd.concat(above_ma_frames, axis=1)
    breadth = combined.mean(axis=1) * 100  # % выше своей 50-дневной
    breadth = breadth.dropna()
    with open(BREADTH_CACHE, 'wb') as f:
        pickle.dump(breadth, f)
    print(f"Готово, {len(above_ma_frames)} тикеров вошли в расчёт.")

# --- та же методология лага, что и в regime_lag_analysis.py / vix_lag_analysis.py ---
PRICE_WINDOW = 10

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
print(f"Найдено разворотных минимумов цены: {len(price_low_dates)}")

breadth_low_idx = find_swing_extrema(breadth, 5, find_max=False)  # минимум breadth = дно участия
breadth_low_dates = [breadth.index[i] for i in breadth_low_idx]
print(f"Найдено минимумов breadth (окно ±5 дн.): {len(breadth_low_dates)}")

SEARCH_RANGE = 25
lags = []
for pd_date in price_low_dates:
    candidates = [(abs((bd - pd_date).days), (bd - pd_date).days) for bd in breadth_low_dates
                  if abs((bd - pd_date).days) <= SEARCH_RANGE]
    if not candidates:
        continue
    candidates.sort()
    lags.append(candidates[0][1])

s = pd.Series(lags)
print(f"\nСовпало с разворотом (в пределах ±{SEARCH_RANGE} дн.): {len(s)} из {len(price_low_dates)}")
print(f"Лаг breadth относительно дна цены (отрицательный = опередил):")
print(s.describe(percentiles=[.1, .25, .5, .75, .9]))
print(f"Опередил (лаг<0): {(s<0).sum()}  Совпал (лаг=0): {(s==0).sum()}  Отстал (лаг>0): {(s>0).sum()}")

print(f"\n--- Для сравнения ---")
print("EMA20: среднее 11.6, медиана 7.5 | Спроецированная EMA20: среднее 4.8, медиана 4.0")
print("Пик VIX: среднее -1.2 (опережает), медиана 0")
