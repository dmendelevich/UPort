# /root/UPort/load_exchanges_part3.py
#!/usr/bin/env python3
import os
import sys
import json
import logging
from pathlib import Path

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.resolve()))

# Импортируем ваш официальный авторизованный инстанс базы из ядра и таймер
from database import db_sys
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_international_exchanges_part3():
    print("\n" + "="*95)
    print("🌏 [GLOBAL EXCHANGES INJECTOR]: Загрузка Части 3 (Азия и Океания) в СУБД...")
    print("="*95)
    
    # Путь к новому файлу справочника Босса
    json_path = "/root/UPort/site_connectors/exchanges_part3_asia_pacific.json"
    
    if not os.path.exists(json_path):
        logging.error(f"❌ Файл третьей части манифеста бирж не найден по пути: {json_path}")
        return

    try:
        logging.info(f"📂 Открываю третью часть спецификации бирж Босса: {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        exchanges_list = data.get("exchanges", [])
        logging.info(f"📊 В файле обнаружено азиатских площадок: {len(exchanges_list)}")
        
        inserted_count = 0
        skipped_count = 0
        
        # Интеллектуальный справочник-маппинг для автоматической разметки на лету
        # Поможет ИИ и синхронизаторам сразу знать коды внешних API для Азии
        av_meta_mapping = {
            "XPHS": {"tv": "PSE", "fb": "EU"},    # Филиппины ➔ шлюз Tradernet EU/Exante
            "XHKG": {"tv": "HKEX", "fb": "HKEX"}   # Гонконг (на случай если он в этой части)
        }
        
        with utils.timer("Импорт азиатских сессий в public.exchanges"):
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
                    
                # Гарантируем наличие азиатской валюты (например, PHP) в public.currencies
                db_sys.ensure_currency(currency.upper())
                
                # Упаковываем метаданные
                metadata_str = json.dumps(ex, ensure_ascii=False)
                safe_metadata_str = metadata_str.replace("'", "''")
                
                # Извлекаем утренние технические коды из нашей карты маппинга
                codes = av_meta_mapping.get(mic, {"tv": code, "fb": "EU"})
                tv_code = codes["tv"]
                fb_market_code = codes["fb"]
                
                # Безопасный реляционный налив биржи с поддержкой tv_code и fb_market_code
                sql_save = f"""
                    INSERT INTO public.exchanges (
                        mic, exchange_code, exchange_name, currency_id, 
                        exchange_timezone, yahoo_suffix, market_metadata, 
                        yahoo_code, tv_code, fb_market_code
                    )
                    VALUES (
                        '{mic}', '{code}', '{name}', '{currency}', 
                        '{timezone}', '{yahoo_suffix}', '{safe_metadata_str}'::jsonb, 
                        '{yahoo_code}', '{tv_code}', '{fb_market_code}'
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
                
        logging.info(f"🏆 [АЗИАТСКИЙ НАЛИВ УСПЕШЕН]: В базу добавлено/обновлено бирж: {inserted_count} шт. (Пропущено: {skipped_count})")
        
    except Exception as err:
        logging.error(f"❌ Критический сбой при парсинге третьей части JSON бирж: {err}")

if __name__ == "__main__":
    load_international_exchanges_part3()
