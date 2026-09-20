"""
Продолжение темы SPMO-корзины / circuit breaker: на кэшах 2024-2026 (бык) и
2020-2022 (медведь) статистика слишком тонкая -- 3 и 2 периода ребалансировки,
в медведе брейкер срабатывал буквально 1 раз. Тянем длинную историю (с запасом
до 2014, чтобы первая ребалансировка ~2015 имела полный 12-мес lookback) --
даёт ~20 периодов ребалансировки вместо 5, статистика становится осмысленной.

Тот же паттерн скачивания и тот же набор колонок, что в combined_sleeves_check.py
(sp500_fundamentals.json -- сегодняшний список S&P 500, yf.download одним
пакетным вызовом) -- для совместимости с уже написанным кодом свипов.

ВАЖНАЯ ОГОВОРКА (честно, не прятать): список тикеров -- сегодняшний состав
S&P 500, спроецированный назад. Компании, выбывшие из индекса за 2014-2026
(поглощения, банкротства, исключения) в выборке отсутствуют -- это
survivorship bias на уровне ВСЕЙ вселенной (не только топ-20, ту дыру мы уже
закрыли point-in-time пересчётом ранжирования). Не идеально, но лучше, чем
было, и это лучшее, что можно сделать без платной подписки на point-in-time
членство в индексе.
"""
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import sys
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings

CACHE_DIR = Path(__file__).resolve().parent / '_cache'
FRAMES_CACHE = CACHE_DIR / 'signal_frames_long_2014_2026.pkl'

with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
symbols = list(fundamentals.keys())

DOWNLOAD_START = '2014-06-01'
DOWNLOAD_END = '2026-09-20'

print(f"Скачиваю {len(symbols)} тикеров, {DOWNLOAD_START} -> {DOWNLOAD_END}...")
raw = yf.download(symbols, start=DOWNLOAD_START, end=DOWNLOAD_END, interval='1d',
                   group_by='ticker', threads=True, progress=True, auto_adjust=False)

frames = {}
skipped = 0
for sym in symbols:
    try:
        df = raw[sym].dropna(subset=['Close'])
    except Exception:
        skipped += 1
        continue
    if df is None or len(df) < 220:
        skipped += 1
        continue
    close, vol, high, low = df['Close'], df['Volume'], df['High'], df['Low']
    vol_pct = close.pct_change().rolling(settings.DAILY_VOLATILITY_WINDOW_DAYS).std() * 100
    turnover_usd = vol * close
    frames[sym] = pd.DataFrame({
        'close': close, 'high': high, 'low': low, 'vol_pct': vol_pct, 'turnover_usd': turnover_usd,
    })

with open(FRAMES_CACHE, 'wb') as f:
    pickle.dump(frames, f)

print(f"Готово: {len(frames)} тикеров сохранено, {skipped} пропущено (недостаточно истории).")
print(f"Файл: {FRAMES_CACHE}")
