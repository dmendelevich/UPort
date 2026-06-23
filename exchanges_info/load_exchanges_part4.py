# /root/UPort/site_connectors/load_exchanges_part4.py
#!/usr/bin/env python3
import os
import sys
import json
import logging
from pathlib import Path

# Жестко поднимаемся на один уровень вверх, чтобы Python гарантированно видел корень /root/UPort/
sys.path.append(str(Path(__file__).parent.parent.resolve()))

# Импортируем ваш официальный авторизованный инстанс базы из ядра и таймер
from database import db_sys
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_international_exchanges_part4():
    print("\n" + "="*95)
    print("🌍 [GLOBAL EXCHANGES INJECTOR]: Загрузка Заключительной Части 4 (Global) в СУБД...")
    print("="*95)
    
    # Путь к четвертому файлу справочника Босса
    json_path = "/root/UPort/site_connectors/exchanges_part4_global.json"
    
    if not os.path.exists(json_path):
        logging.error(f"❌ Файл четвертой части манифеста бирж не найден по пути: {json_path}")
        return

    try:
        logging.info(f"📂 Открываю четвертую часть спецификации бирж Босса: {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        exchanges_list = data.get("exchanges", [])
        logging.info(f"📊 В файле обнаружено международных площадок: {len(exchanges_list)}")
        
        inserted_count = 0
        skipped_count = 0
        
        with utils.timer("Импорт глобальных сессий в public.exchanges"):
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
                    
                # Автоматически гарантируем наличие валюты в public.currencies
                db_sys.ensure_currency(currency.upper())
                
                # Упаковываем метаданные в jsonb
                metadata_str = json.dumps(ex, ensure_ascii=False)
                safe_metadata_str = metadata_str.replace("'", "''")
                
                # По дефолту для экзотических мировых площадок ставим глобальный контур брокера 'EU' (Exante)
                # Если в этой части будут площадки США или Казахстана, мы переназначим их SQL-запросом ниже
                fb_market_code = "EU"
                if mic in ("XNAS", "XNYS", "ARCX", "IEXG", "BATS"):
                    fb_market_code = "FIX"
                elif mic == "KASE":
                    fb_market_code = "KASE"
                
                sql_save = f"""
                    INSERT INTO public.exchanges (
                        mic, exchange_code, exchange_name, currency_id, 
                        exchange_timezone, yahoo_suffix, market_metadata, 
                        yahoo_code, tv_code, fb_market_code
                    )
                    VALUES (
                        '{mic}', '{code}', '{name}', '{currency}', 
                        '{timezone}', '{yahoo_suffix}', '{safe_metadata_str}'::jsonb, 
                        '{yahoo_code}', '{code}', '{fb_market_code}'
                    )
                    ON CONFLICT (mic) 
                    DO UPDATE SET 
                        exchange_code = EXCLUDED.exchange_code,
                        exchange_name = EXCLUDED.exchange_name,
                        currency_id = EXCLUDED.currency_id,
                        exchange_timezone = EXCLUDED.exchange_timezone,
                        yahoo_suffix = EXCLUDED.yahoo_suffix,
                        market_metadata = EXCLUDED.market_metadata,
                        yahoo_code = EXCLUDED.yahoo_code,
                        tv_code = COALESCE(public.exchanges.tv_code, EXCLUDED.tv_code),
                        fb_market_code = COALESCE(public.exchanges.fb_market_code, EXCLUDED.fb_market_code);
                """
                db_sys.execute_query(sql_save)
                inserted_count += 1
                
        logging.info(f"🏆 [ГЛОБАЛЬНЫЙ НАЛИВ УСПЕШЕН]: В базу добавлено/обновлено бирж: {inserted_count} шт. (Пропущено: {skipped_count})")
        
    except Exception as err:
        logging.error(f"❌ Критический сбой при парсинге четвертой части JSON бирж: {err}")

if __name__ == "__main__":
    load_international_exchanges_part4()
