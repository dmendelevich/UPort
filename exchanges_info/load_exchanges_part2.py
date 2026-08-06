#!/usr/bin/env python3
import os
import sys
import json
import logging
from pathlib import Path

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

# Импортируем ваш официальный авторизованный инстанс базы из ядра
from database import db_sys
# Импортируем утилиты для красивого замера времени
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_international_exchanges_part2():
    print("\n" + "="*95)
    print("🇪🇺 [GLOBAL EXCHANGES INJECTOR]: Загрузка Части 2 (Европа / Т212) в СУБД...")
    print("="*95)
    
    # Автоматический поиск второй части манифеста в вашей папке сканирования
    possible_paths = [
        "/root/UPort/site_connectors/exchanges_part2_eu.json",
        "/root/UPort/site_connectors/exchanges_part2.json"
    ]
    
    json_path = None
    for path in possible_paths:
        if os.getenv("DB_HOST") or os.path.exists(path): # Предохранитель путей
            if os.path.exists(path):
                json_path = path
                break
            
    if not json_path:
        folder = Path("/root/UPort/site_connectors")
        for f in folder.glob("*part2*.json"):
            json_path = str(f)
            break

    if not json_path or not os.path.exists(json_path):
        logging.error("❌ Файл второй части манифеста бирж не найден в папке /root/UPort/site_connectors/")
        return

    try:
        logging.info(f"📂 Открываю вторую часть спецификации бирж Босса: {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        exchanges_list = data.get("exchanges", [])
        logging.info(f"📊 В файле обнаружено международных площадок: {len(exchanges_list)}")
        
        inserted_count = 0
        skipped_count = 0
        
        with utils.timer("Импорт европейских сессий в public.exchanges"):
            for ex in exchanges_list:
                mic = ex.get("mic")
                code = ex.get("code")
                name = ex.get("name")
                timezone = ex.get("timezone")
                currency = ex.get("currency")
                yahoo_suffix = ex.get("default_data_suffix", "")
                yahoo_code = ex.get("yahoo_code", ex.get("yahoo_exchange", code))
                is_enabled = ex.get("enabled", True)
                
                if not mic or not currency:
                    continue
                    
                if not is_enabled:
                    skipped_count += 1
                    continue
                    
                # 🔥 НАШЕ РОДНОЕ РЕШЕНИЕ ЯДРА: Гарантируем наличие валюты в public.currencies
                # Скрипт вызывает ваш официальный метод из database.py перед вставкой биржи!
                db_sys.ensure_currency(currency.upper())
                
                # Упаковываем сессии торгов, DST и weekmask в jsonb
                metadata_str = json.dumps(ex, ensure_ascii=False)

                # Безопасный реляционный налив биржи
                sql_save = """
                    INSERT INTO public.exchanges (mic, exchange_code, exchange_name, currency_id, exchange_timezone, yahoo_suffix, market_metadata, yahoo_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (mic)
                    DO UPDATE SET
                        exchange_code = EXCLUDED.exchange_code,
                        exchange_name = EXCLUDED.exchange_name,
                        currency_id = EXCLUDED.currency_id,
                        exchange_timezone = EXCLUDED.exchange_timezone,
                        yahoo_suffix = EXCLUDED.yahoo_suffix,
                        market_metadata = EXCLUDED.market_metadata,
                        yahoo_code = EXCLUDED.yahoo_code;
                """
                db_sys.execute_query(sql_save, (mic, code, name, currency, timezone, yahoo_suffix, metadata_str, yahoo_code))
                inserted_count += 1
                
        logging.info(f"🏆 [МЕЖДУНАРОДНЫЙ НАЛИВ УСПЕШЕН]: В базу добавлено/обновлено бирж: {inserted_count} шт. (Пропущено: {skipped_count})")
        
    except Exception as err:
        logging.error(f"❌ Критический сбой при парсинге второй части JSON бирж: {err}")

if __name__ == "__main__":
    load_international_exchanges_part2()