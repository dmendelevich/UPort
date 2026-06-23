#!/usr/bin/env python3
import os
import sys
import logging
import json
from pathlib import Path
from dotenv import load_dotenv
import yfinance as yf

# Подгружаем пути ядра UPort
sys.path.append(str(Path(__file__).parent.resolve()))
from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_universal_input_parser(raw_string: str, fb_client: FreedomBrokerClient) -> dict:
    """
    УНИВЕРСАЛЬНЫЙ ВХОДНОЙ ШЛЮЗ UPort v3.0 (ТАБЛИЧНЫЙ МОСТ FB_MARKETS).
    """
    clean_sym = str(raw_string).strip()
    if not clean_sym:
        logging.error("❌ На входе пустая строка.")
        return None

    logging.info(f"🔮 [КОНВЕЙЕР СТАРТ]: Анализ входящей строки: '{clean_sym}'")
    
    target_mic = None
    target_exchange_code = None
    target_currency_id = None
    detection_method = "NOT_DETECTED"
    
    # 🔥 ЭТАП 1: ОПРЕДЕЛЕНИЕ МЕЖДУНАРОДНЫХ СУФФИКСОВ ПО EXCHANGES
    sql_yahoo_suffixes = "SELECT mic, exchange_code, yahoo_suffix, currency_id FROM public.exchanges WHERE yahoo_suffix IS NOT NULL AND yahoo_suffix != '';"
    db_yahoo_suffixes = db_sys.execute_query(sql_yahoo_suffixes)
    
    if db_yahoo_suffixes and isinstance(db_yahoo_suffixes, list):
        for row in db_yahoo_suffixes:
            sfx = str(row["yahoo_suffix"]).strip()
            if clean_sym.upper().endswith(sfx.upper()):
                target_mic = row["mic"]
                target_exchange_code = row["exchange_code"]
                target_currency_id = row["currency_id"]
                detection_method = f"DATABASE_YAHOO_SUFFIX_MATCH ({target_exchange_code})"
                clean_sym = clean_sym[: -len(sfx)]
                logging.info(f"   🎯 [ФИЛЬТР СУФФИКСОВ]: Найдена биржа '{sfx}'. Отрезан. Тело: '{clean_sym}', MIC: {target_mic}")
                break

    # Отсекаем брокерскую шелуху шлюза EXANTE, если тикер пришел в формате брокера
    if not target_mic and clean_sym.upper().endswith(".US"):
        clean_sym = clean_sym[: -3]
        logging.info(f"   🎯 [ФИЛЬТР БРОКЕРА]: Обнаружен шлюз '.US'. Отрезан. Тело: '{clean_sym}'")

    symbol_uport = clean_sym.upper()
    logging.info(f"   📦 [ИТОГ ОЧИСТКИ]: Символ для таблицы tickers = '{symbol_uport}'")

    # 🔥 ЭТАП 2: СЕТЕВАЯ РАЗВЕДКА YAHOO (Если суффикса не было)
    if "DATABASE_YAHOO_SUFFIX_MATCH" in detection_method:
        sql_get_sfx = f"SELECT yahoo_suffix FROM public.exchanges WHERE mic = '{target_mic}';"
        res_sfx = db_sys.execute_query(sql_get_sfx)
        yahoo_suffix_str = res_sfx[0]["yahoo_suffix"] if res_sfx else ""
        yahoo_symbol_request = f"{symbol_uport}{yahoo_suffix_str.upper()}"
    else:
        yahoo_symbol_request = symbol_uport.replace('.', '-')

    yf_exchange_code = "UNKNOWN"
    if not target_mic:
        try:
            logging.info(f"   📡 [YAHOO СЕТЬ]: Запрос паспорта для '{yahoo_symbol_request}'...")
            ticker_obj = yf.Ticker(yahoo_symbol_request)
            yf_exchange_code = ticker_obj.fast_info.get("exchange")
            
            if not yf_exchange_code or yf_exchange_code == "Unknown":
                yf_exchange_code = "UNKNOWN"
            else:
                logging.info(f"   📥 [YAHOO ОТВЕТ]: Код биржи от Yahoo: '{yf_exchange_code}'")

            sql_lookup_mic = f"""
                SELECT mic, exchange_code, currency_id 
                FROM public.exchanges 
                WHERE yahoo_code = '{yf_exchange_code}' 
                   OR exchange_code = '{yf_exchange_code}' 
                ORDER BY (mic = 'XNAS' OR mic = 'XNYS') DESC LIMIT 1;
            """
            match_rows = db_sys.execute_query(sql_lookup_mic)
            
            if match_rows and isinstance(match_rows, list) and len(match_rows) > 0:
                target_mic = match_rows[0]["mic"]
                target_exchange_code = match_rows[0]["exchange_code"]
                target_currency_id = match_rows[0]["currency_id"]
                detection_method = f"YAHOO_NETWORK_MATCH ({yf_exchange_code})"
                logging.info(f"   ✅ [МАТЧИНГ СУБД]: Код Yahoo сопоставлен. MIC: {target_mic} ({target_exchange_code})")
        except Exception as network_err:
            logging.error(f"   🚨 Сбой yfinance: {network_err}")
            yf_exchange_code = "ERROR"


    # =======================================================================================================
    # ЭТАП 3: СЕТЕВОЙ ПЕРЕВОД НА ЯЗЫК FREEDOM BROKER ЧЕРЕЗ ТАБЛИЦУ public.fb_markets
    # =======================================================================================================
    fb_ticker_result = "NULL"
    fb_market_detected = "NULL"
    fb_exchange_detected = "NULL"
    
    try:
        logging.info(f"   🔍 [ФРИДОМ СЕТЬ]: Поиск find_ticker('{symbol_uport}')...")
        found_data = fb_client.find_ticker(symbol_uport)
        
        if found_data and isinstance(found_data, list):
            for item in found_data:
                exchange_ticker = str(item.get("code_nm", "")).strip().upper()
                broker_market_code = str(item.get("mkt", "")).strip().upper()
                
                # Проверяем совпадение базового тела тикера (до точки)
                if exchange_ticker == symbol_uport or ('.' in symbol_uport and exchange_ticker == symbol_uport.split('.')[0]):
                    
                    # ИДЕМ В НАШУ НОВУЮ ТАБЛИЦУ РЫНКОВ ДЛЯ ПРОВЕРКИ МОСТА
                    sql_mkt_bridge = f"""
                        SELECT uport_default_mic, market_name 
                        FROM public.fb_markets 
                        WHERE fb_market_code = '{broker_market_code}' 
                           OR fb_short_code = '{broker_market_code}'
                        LIMIT 1;
                    """
                    mkt_bridge_rows = db_sys.execute_query(sql_mkt_bridge)
                    
                    is_exchange_match = False
                    if mkt_bridge_rows and isinstance(mkt_bridge_rows, list) and len(mkt_bridge_rows) > 0:
                        bridge = mkt_bridge_rows[0]
                        bridge_mic = bridge.get("uport_default_mic")
                        fb_market_detected = str(bridge.get("market_name")).strip().upper()
                        
                        # Если MIC совпал напрямую (например XTSE == XTSE для Канады)
                        if bridge_mic and target_mic and bridge_mic.upper() == target_mic.upper():
                            is_exchange_match = True
                        
                        # Обработка США (рынок FIX): одобряем, если целевая биржа из США (XNAS, XNYS, ARCX, IEXG)
                        elif broker_market_code == "FIX" and target_mic in ["XNAS", "XNYS", "ARCX", "IEXG"]:
                            is_exchange_match = True
                            
                        # Обработка Европы/Великобритании (рынок EU): одобряем для XLON
                        elif broker_market_code == "EU" and target_mic == "XLON":
                            is_exchange_match = True

                    if is_exchange_match:
                        fb_ticker_result = str(item.get("t", "")).strip().upper()
                        fb_exchange_detected = str(item.get("codesub_nm", "")).strip().upper() or "NOT_PROVIDED"
                        logging.info(f"   ✅ [ТАБЛИЧНЫЙ МОСТ УСПЕШЕН]: Рынок брокера '{broker_market_code}' сопоставлен с нашей биржей!")
                        break
            
            if fb_ticker_result == "NULL":
                logging.warning(f"   ❌ [ОТКАЗ]: Брокер вернул инструменты, но рынок '{broker_market_code}' не совпал с целевым MIC '{target_mic}' в СУБД UPort.")
        else:
            logging.warning(f"   ⚠️ Брокер вернул пустой ответ на поиск.")
            
    except Exception as fb_err:
        logging.error(f"   🚨 Ошибка переводчика FB: {fb_err}")

    # =======================================================================================================
    # ФИНАЛЬНЫЙ ВЫВОД ПАСПОРТА
    # =======================================================================================================
    return {
        "UPORT_SYMBOL": symbol_uport,
        "YAHOO_SYMBOL": yahoo_symbol_request,
        "DETERMINED_MIC": target_mic,
        "EXCHANGE_CODE_UPORT": target_exchange_code,
        "CURRENCY_ID": target_currency_id,
        "DETECTION_METHOD": detection_method,
        "YAHOO_RAW_EXCHANGE": yf_exchange_code,
        "FB_TICKER_PASSPORT": fb_ticker_result,
        "FB_MARKET_NAME": fb_market_detected,
        "FB_EXCHANGE_NAME": fb_exchange_detected
    }

if __name__ == "__main__":
    print("\n" + "="*110)
    print("🕵️‍♂️ [UPort UNIVERSAL PARSER v3.0]: Контрольный прогон по таблице fb_markets...")
    print("="*110 + "\n")

    env_path = Path(__file__).parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub = os.getenv("FB_DLM_API_KEY")
    fb_priv = os.getenv("FB_DLM_API_SECRET")
    
    if not fb_pub or not fb_priv:
        print("❌ Ошибка: Ключи API не найдены!")
        sys.exit(1)
        
    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)

    # Тест 1: Канадская бумага HBM.TO (Ожидаем NULL, так как у ФБ на рынке TSX нет такой бумаги)
    case_1 = test_universal_input_parser("HBM.TO", fb_client)
    print(f"📋 ПАСПОРТ HBM.TO:\n{json.dumps(case_1, indent=4, ensure_ascii=False)}\n" + "-"*110)

    # Тест 2: Американский финтех XYZ (Ожидаем XYZ.US на рынке NYSE/NASDAQ через FIX-мост)
    case_2 = test_universal_input_parser("XYZ", fb_client)
    print(f"📋 ПАСПОРТ XYZ:\n{json.dumps(case_2, indent=4, ensure_ascii=False)}\n" + "-"*110)

    # Тест 3: Лондонская акция ANTO.L (Ожидаем ANTO.EU на рынке EU Европа через EU-мост)
    case_3 = test_universal_input_parser("ANTO.L", fb_client)
    print(f"📋 ПАСПОРТ ANTO.L:\n{json.dumps(case_3, indent=4, ensure_ascii=False)}\n" + "="*110)


