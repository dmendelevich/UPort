#!/usr/bin/env python3
import os
import sys
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

# Импортируем ваш официальный авторизованный инстанс базы из ядра
from database import db_sys
# Импортируем наши новые глобальные утилиты UPort
import utils

import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 🌍 ГЛОБАЛЬНЫЕ НАСТРОЙКИ СКАНИРОВАНИЯ ИНДЕКСОВ (GITHUB DATA HUB)
GITHUB_INDEX_SOURCES = [
    {
        "index_name": "SP500",
        "url": "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
        "symbol_column": "Symbol"
    }
]

class StockDonorsScanner:
    """Контур А: Парсер акций-доноров из открытых индексов напрямую в Tickers."""
    def __init__(self, db_instance):
        self.db_instance = db_instance

    def execute_sync(self):
        logging.info("🚀 [STOCK SCANNER]: Запуск каскадного обновления корзин акций-доноров...")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        timestamp_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        for source in GITHUB_INDEX_SOURCES:
            index_name = source["index_name"]
            url = source["url"]
            sym_col = source["symbol_column"]
            
            try:
                response = requests.get(url, headers=headers, timeout=15)
                response.raise_for_status()
                df = pd.read_csv(io.StringIO(response.text))
                
                if df.empty or sym_col not in df.columns:
                    logging.warning(f"⚠️ [Контур {index_name}]: Получена пустая таблица или отсутствует колонка {sym_col}")
                    continue
                
                raw_tickers = df[sym_col].dropna().unique().tolist()
                logging.info(f"📊 [Контур {index_name}]: Извлечено {len(raw_tickers)} уникальных тикеров из первоисточника.")
                
                inserted_count = 0
                
                # Перебираем тикеры и вызываем наш элитный реляционный паспортный контроль
                for raw_sym in raw_tickers:
                    clean_sym = str(raw_sym).strip()
                    if not clean_sym: 
                        continue
                        
                    passport = utils.detect_ticker_passport(self.db_instance, clean_sym)
                    if not passport:
                        continue
                        
                    yahoo_symbol = passport["yahoo_symbol"]
                    mic = passport["mic"]
                    
                    # 🔥 НАШ КВАНТОВЫЙ UPDATE: Вживляем 'MS_SP500', сохраняя старую дату первичного залета!
                    sql_save = """
                        INSERT INTO public.tickers (symbol, yahoo_symbol, exchange_mic, provenance)
                        VALUES (%s, %s, %s, jsonb_build_object('MS_SP500', %s))
                        ON CONFLICT (symbol)
                        DO UPDATE SET
                            provenance = jsonb_set(
                                public.tickers.provenance,
                                '{MS_SP500}',
                                to_jsonb(%s::text),
                                false
                            ),
                            yahoo_symbol = EXCLUDED.yahoo_symbol,
                            exchange_mic = EXCLUDED.exchange_mic;
                    """
                    self.db_instance.execute_query(sql_save, (clean_sym, yahoo_symbol, mic, timestamp_str, timestamp_str))
                    inserted_count += 1
                    
                logging.info(f"✅ [Контур {index_name}]: Влить в tickers: {inserted_count} шт. Провожу дельта-очистку вылетевших...")
                
                # 🔥 ДЕЛЬТА-ОЧИСТКА ВЫЛЕТЕВШИХ: Снайперски срезаем ключ у тех, кого больше нет в индексе!
                # NOT IN со списком не работает через JSON-параметризацию (список становится
                # ARRAY[...], не набором для IN) -- NOT (x = ANY(%s)) вместо NOT IN (Claude/BACKLOG.md №81).
                sql_delta_cleanup = """
                    UPDATE public.tickers
                    SET provenance = provenance - 'MS_SP500'
                    WHERE provenance ? 'MS_SP500' AND NOT (symbol = ANY(%s));
                """
                self.db_instance.execute_query(sql_delta_cleanup, ([str(ts) for ts in raw_tickers],))
                logging.info(f"🧹 [Контур {index_name}]: Дельта-очистка вылетевших акций успешно завершена.")
                
            except Exception as err:
                logging.error(f"❌ [Контур {index_name} СБОЙ]: Ошибка при обработке индекса: {err}")


class EtfLeadersScanner:
    """Контур Б: Локальный инжектор фондов из Asset Specification JSON Босса напрямую в Tickers."""
    def __init__(self, db_instance, json_path: str):
        self.db_instance = db_instance
        self.json_path = json_path

    def execute_sync(self):
        logging.info(f"🚀 [ETF SCANNER]: Запуск парсинга манифеста фондов из файла: {self.json_path}")
        timestamp_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        if not os.path.exists(self.json_path):
            logging.error(f"❌ [ETF SCANNER КРИТИЧЕСКИЙ СБОЙ]: Файл конфигурации не найден по пути {self.json_path}")
            return

        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            assets_list = data.get("assets", [])
            logging.info(f"📊 В манифесте обнаружено всего записей: {len(assets_list)}")
            
            inserted_count = 0
            active_etfs = []
            
            for asset in assets_list:
                ticker = asset.get("ticker")
                is_enabled = asset.get("enabled", True)
                
                if not ticker or not is_enabled:
                    continue
                
                clean_ticker = str(ticker).strip()
                active_etfs.append(clean_ticker)
                
                passport = utils.detect_ticker_passport(self.db_instance, clean_ticker)
                if not passport:
                    continue
                    
                yahoo_symbol = passport["yahoo_symbol"]
                mic = passport["mic"]
                
                asset_json_str = json.dumps(asset, ensure_ascii=False)

                # 🔥 КВАНТОВЫЙ UPDATE ФОНДОВ: Вживляем 'MS_TOP100_ETF' и сохраняем его вложенный JSON!
                sql_save = """
                    INSERT INTO public.tickers (symbol, yahoo_symbol, exchange_mic, asset_metadata, provenance)
                    VALUES (%s, %s, %s, %s::jsonb, jsonb_build_object('MS_TOP100_ETF', %s))
                    ON CONFLICT (symbol)
                    DO UPDATE SET
                        provenance = jsonb_set(
                            public.tickers.provenance,
                            '{MS_TOP100_ETF}',
                            to_jsonb(%s::text),
                            false
                        ),
                        asset_metadata = EXCLUDED.asset_metadata,
                        yahoo_symbol = EXCLUDED.yahoo_symbol,
                        exchange_mic = EXCLUDED.exchange_mic;
                """
                self.db_instance.execute_query(sql_save, (
                    clean_ticker, yahoo_symbol, mic, asset_json_str, timestamp_str, timestamp_str
                ))
                inserted_count += 1
                
            logging.info(f"✅ [ETF SCANNER]: Влито фондов в tickers: {inserted_count} шт. Запускаю дельта-очистку...")
            
            # 🔥 ДЕЛЬТА-ОЧИСТКА ВЫЛЕТЕВШИХ ФОНДОВ:
            sql_etf_cleanup = """
                UPDATE public.tickers
                SET provenance = provenance - 'MS_TOP100_ETF', asset_metadata = NULL
                WHERE provenance ? 'MS_TOP100_ETF' AND NOT (symbol = ANY(%s));
            """
            self.db_instance.execute_query(sql_etf_cleanup, (active_etfs,))
            logging.info("🧹 [ETF SCANNER]: Дельта-очистка вылетевших фондов успешно завершена.")
            
        except Exception as err:
            logging.error(f"❌ [ETF SCANNER СБОЙ]: Ошибка при наливе JSON: {err}")


def main():
    CUSTOM_JSON_PATH = "/root/UPort/site_connectors/etf_universe.json"
    
    # 🚨 ВНИМАНИЕ: Мы НЕ смываем Master-таблицу tickers, так как работаем в экономном дельта-режиме!
    # Запускаем конвейер
    stock_scanner = StockDonorsScanner(db_sys)
    stock_scanner.execute_sync()
    
    etf_scanner = EtfLeadersScanner(db_sys, CUSTOM_JSON_PATH)
    etf_scanner.execute_sync()

if __name__ == "__main__":
    main()
