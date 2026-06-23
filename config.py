# /root/UPort/config.py
"""
Системный манифест архитектуры UPort.
Здесь хранятся технические параметры, настройки потоков, пачек (чанков) и путей.
"""
import os
from pathlib import Path

# ─── СИСТЕМНЫЕ ПУТИ И ДИРЕКТОРИИ ───
BASE_DIR = Path(__file__).parent.resolve()
ETF_UNIVERSE_JSON_PATH = os.path.join(BASE_DIR, "site_connectors", "etf_universe.json")

# ─── НАСТРОЙКИ СЕТЕВЫХ ПОТОКОВ И ПАЧЕК (ЧАНКОВ) ───
# Настройки для ночного сбора ликвидности (Yahoo Finance)
CHUNK_SIZE_TURNOVER = 100
PAUSE_TURNOVER_SEC = 1.5

# Настройки для утреннего сбора технических сигналов и маркеров тренда
CHUNK_SIZE_SIGNALS = 50
PAUSE_SIGNALS_SEC = 0.5  # Пауза между пакетными запросами к брокеру
# 🔥 СУПЕРЧАНКИ БОССА: Защита от бана 429 при расширении Universe (оход ограничения 10 запросов в 1 минуту)
SUPERCHUNK_SIZE_SIGNALS = 500 # Делим планетарный рынок на блоки по 500 акций
SUPERCHUNK_PAUSE_SEC = 65.0   # Глубокая минутная суперпауза для полного сброса лимитов API

# Барьер колебаний средних цен для самолечения истории цен при сплитах: 
CRITICAL_SPLIT_PCT = 40.0

# ─── СИСТЕМНЫЕ ТАЙМАУТЫ (ЗАЩИТА ОТ ЗАВИСАНИЯ) ───
NETWORK_TIMEOUT_SEC = 25
