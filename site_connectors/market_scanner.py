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
                    
                    # Вживляем 'MS_SP500' -- create_missing=true (было false, Claude/BACKLOG.md №90,
                    # находка M): для тикера, УЖЕ существовавшего в tickers до прогона сканера
                    # (подавляющее большинство), jsonb_set с false молча не создавал ключ вообще.
                    #
                    # SELECT-затем-UPDATE/INSERT, не ON CONFLICT (Claude/BACKLOG.md №128) -- жгло
                    # sequence на КАЖДОМ уже существующем тикере при каждом прогоне (подавляющее
                    # большинство S&P500 уже в базе после первого раза) -- крупнейшая находка
                    # инвентаря (~7000 спалено). INSERT с RETURNING на случай гонки с живым
                    # ensure_ticker_v3 (не сериализовано, в отличие от accounts) -- проиграли,
                    # execute_query молча глотает ошибку уникальности (№81), добираем id повторным
                    # SELECT.
                    existing_row = self.db_instance.execute_row(
                        "SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;", (clean_sym,)
                    )
                    if existing_row:
                        sql_update = """
                            UPDATE public.tickers
                            SET provenance = jsonb_set(provenance, '{MS_SP500}', to_jsonb(%s::text), true),
                                yahoo_symbol = %s,
                                exchange_mic = %s
                            WHERE id = %s;
                        """
                        self.db_instance.execute_query(sql_update, (timestamp_str, yahoo_symbol, mic, int(existing_row["id"])))
                    else:
                        sql_insert = """
                            INSERT INTO public.tickers (symbol, yahoo_symbol, exchange_mic, provenance)
                            VALUES (%s, %s, %s, jsonb_build_object('MS_SP500', %s))
                            RETURNING id;
                        """
                        ins_res = self.db_instance.execute_query(sql_insert, (clean_sym, yahoo_symbol, mic, timestamp_str))
                        if not ins_res:
                            # Проиграли гонку -- строку уже создал кто-то другой между нашим SELECT и INSERT.
                            row_id = self.db_instance.execute_row("SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;", (clean_sym,))
                            if row_id:
                                sql_update = """
                                    UPDATE public.tickers
                                    SET provenance = jsonb_set(provenance, '{MS_SP500}', to_jsonb(%s::text), true),
                                        yahoo_symbol = %s,
                                        exchange_mic = %s
                                    WHERE id = %s;
                                """
                                self.db_instance.execute_query(sql_update, (timestamp_str, yahoo_symbol, mic, int(row_id["id"])))
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

                # Вживляем 'MS_TOP100_ETF' -- create_missing=true (была та же ошибка, что и у
                # MS_SP500 выше, Claude/BACKLOG.md №90, находка M).
                #
                # SELECT-затем-UPDATE/INSERT, не ON CONFLICT (Claude/BACKLOG.md №128) -- тот же
                # приём, что и у MS_SP500 выше (см. комментарий там про сожжённый sequence и
                # гонку с ensure_ticker_v3).
                existing_row = self.db_instance.execute_row(
                    "SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;", (clean_ticker,)
                )
                if existing_row:
                    sql_update = """
                        UPDATE public.tickers
                        SET provenance = jsonb_set(provenance, '{MS_TOP100_ETF}', to_jsonb(%s::text), true),
                            asset_metadata = %s::jsonb,
                            yahoo_symbol = %s,
                            exchange_mic = %s
                        WHERE id = %s;
                    """
                    self.db_instance.execute_query(sql_update, (timestamp_str, asset_json_str, yahoo_symbol, mic, int(existing_row["id"])))
                else:
                    sql_insert = """
                        INSERT INTO public.tickers (symbol, yahoo_symbol, exchange_mic, asset_metadata, provenance)
                        VALUES (%s, %s, %s, %s::jsonb, jsonb_build_object('MS_TOP100_ETF', %s))
                        RETURNING id;
                    """
                    ins_res = self.db_instance.execute_query(sql_insert, (clean_ticker, yahoo_symbol, mic, asset_json_str, timestamp_str))
                    if not ins_res:
                        row_id = self.db_instance.execute_row("SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;", (clean_ticker,))
                        if row_id:
                            sql_update = """
                                UPDATE public.tickers
                                SET provenance = jsonb_set(provenance, '{MS_TOP100_ETF}', to_jsonb(%s::text), true),
                                    asset_metadata = %s::jsonb,
                                    yahoo_symbol = %s,
                                    exchange_mic = %s
                                WHERE id = %s;
                            """
                            self.db_instance.execute_query(sql_update, (timestamp_str, asset_json_str, yahoo_symbol, mic, int(row_id["id"])))
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
