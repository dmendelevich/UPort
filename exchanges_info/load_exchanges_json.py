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
# Импортируем наши глобальные утилиты для контроля времени
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_uport_exchanges_manifest():
    print("\n" + "="*95)
    print("📡 [EXCHANGES INJECTOR]: Загрузка манифеста мировых бирж Босса в СУБД...")
    print("="*95)
    
    # 🔥 ТОЧНЫЙ ПУТЬ К ВАШЕМУ ПЕРВОМУ JSON-ФАЙЛУ С БИРЖАМИ США
    json_path = "/root/UPort/site_connectors/exchanges_part1_us.json"
    
    if not os.path.exists(json_path):
        logging.error(f"❌ Файл манифеста не найден по пути: {json_path}")
        return

    try:
        logging.info(f"📂 Читаю файл конфигурации: {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        exchanges_list = data.get("exchanges", [])
        logging.info(f"📊 Обнаружено записей в текущем файле: {len(exchanges_list)}")
        
        inserted_count = 0
        skipped_count = 0
        
        # Запускаем профайлер времени из utils
        with utils.timer("Импорт спецификации бирж в public.exchanges"):
            for ex in exchanges_list:
                mic = ex.get("mic")
                code = ex.get("code")
                name = ex.get("name")
                timezone = ex.get("timezone")
                currency = ex.get("currency")
                # Извлекаем суффикс данных Yahoo (в вашем JSON это default_data_suffix)
                yahoo_suffix = ex.get("default_data_suffix", "")
                is_enabled = ex.get("enabled", True)
                
                if not mic:
                    continue
                    
                # Фильтруем отключенные биржи, если взведен флаг enabled: false
                if not is_enabled:
                    skipped_count += 1
                    continue
                    
                # 🔥 КВАНТОВАЯ УПАКОВКА: Схлопываем весь объект (включая сессии, DST, weekmask)
                # в чистую JSON-строку для записи в ячейку jsonb СУБД!
                metadata_str = json.dumps(ex, ensure_ascii=False)

                # Бронебойный SQL-запрос налива с каскадным апдейтом
                sql_save = """
                    INSERT INTO public.exchanges (mic, exchange_code, exchange_name, currency_id, exchange_timezone, yahoo_suffix, market_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (mic)
                    DO UPDATE SET
                        exchange_code = EXCLUDED.exchange_code,
                        exchange_name = EXCLUDED.exchange_name,
                        currency_id = EXCLUDED.currency_id,
                        exchange_timezone = EXCLUDED.exchange_timezone,
                        yahoo_suffix = EXCLUDED.yahoo_suffix,
                        market_metadata = EXCLUDED.market_metadata;
                """
                db_sys.execute_query(sql_save, (mic, code, name, currency, timezone, yahoo_suffix, metadata_str))
                inserted_count += 1
                print(f"   🏛️  Площадка {mic:4} ({code:7}) ➡️ Успешно залита! Валюта: {currency} | Таймзона: {timezone}")
                
        logging.info(f"🏆 [ИМПОРТ ЗАВЕРШЕН]: В таблицу public.exchanges налито: {inserted_count} шт. (Пропущено: {skipped_count})")
        
        # Делаем красивую контрольную проверку
        sql_check = "SELECT mic, exchange_code, exchange_timezone, (market_metadata->'sessions'->'regular'->>'open') AS reg_open FROM public.exchanges;"
        check_rows = db_sys.execute_query(sql_check)
        
        print("\n📊 КОНТРОЛЬНЫЙ СРЕЗ ЗАЛИТЫХ БИРЖ ИЗ БАЗЫ ДАННЫХ:")
        print("-"*95)
        for row in check_rows:
            print(f"  ✅ MIC: {row['mic']:4} | Код: {row['exchange_code']:6} | Зона Крона: {row['exchange_timezone']:18} | Старт сессии: {row['reg_open']}")
        print("-"*95 + "\n")
        
    except Exception as err:
        logging.error(f"❌ Критический сбой при наливе JSON-бирж: {err}")

if __name__ == "__main__":
    load_uport_exchanges_manifest()