#!/usr/bin/env python3
import sys
import logging
import json
from pathlib import Path
import yfinance as yf

# Подтягиваем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.resolve()))

from database import db_sys
import utils

# Настраиваем логирование прогресса
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_global_asset_type_migration():
    print("\n" + "="*110)
    logging.info("🚀 [UPort TYPE MIGRATION]: Старт генеральной пакетной разметки Universe...")
    print("="*110)

    # 1. 📡 ВЫГРЕБАЕМ ВЕСЬ НЕРАЗМЕЧЕННЫЙ UNIVERSE СУБД
    sql_get_undefined = """
        SELECT id, symbol, ticker_name_map ->> 'YAHOO' AS yahoo_symbol 
        FROM public.tickers 
        WHERE asset_type = 'UNDEFINED' OR asset_type IS NULL;
    """
    raw_rows = db_sys.execute_query(sql_get_undefined)
    
    if not raw_rows or not isinstance(raw_rows, list):
        logging.info("ℹ️ Неразмеченных паспортов со статусом UNDEFINED в СУБД не обнаружено. Миграция не требуется.")
        return

    # Защитный фильтр: берем только те строки, где имя для Yahoo физически заполнено в JSON-карте
    valid_tasks = [row for row in raw_rows if row.get("yahoo_symbol")]
    total_tickers = len(valid_tasks)
    
    logging.info(f"📊 Обнаружено {total_tickers} активов для извлечения типов. Нарезаю на эшелоны...")

    # Нарезаем массив задач на бережные эшелоны по 40 акций в пачке (безопасно для Yahoo)
    echelons = list(utils.create_echelons(valid_tasks, chunk_size=40))
    total_echelons = len(echelons)
    logging.info(f"📦 Сформировано {total_echelons} эшелонов для пакетной REST-загрузки.")

    # ─── ШАГ 2: НЕУБИВАЕМЫЙ ПОСТРОЧНЫЙ КОНВЕЙЕР РАЗМЕТКИ (БЕЗ ПАКЕТНЫХ СБОЕВ) ───
    processed_count = 0
    updated_count = 0

    for row in valid_tasks:
        processed_count += 1
        row_id = int(row["id"])
        uport_symbol = str(row["symbol"]).strip()
        yahoo_symbol = str(row["yahoo_symbol"]).strip().upper()
        
        detected_type = "UNDETERMINED"
        logging.info(f"🌐 [{processed_count}/{total_tickers}]: Анализ актива {uport_symbol} (Yahoo: {yahoo_symbol})...")
        
        try:
            # Выполняем строго индивидуальный, изолированный вызов (как в Главных Воротах)
            ticker_obj = yf.Ticker(yahoo_symbol)
            
            # Попытка №1: Прямое чтение типа через fast_info контур
            raw_type = ticker_obj.fast_info.get("quoteType")
            if raw_type:
                detected_type = str(raw_type).strip().upper()
            else:
                # Попытка №2: Если fast_info промолчал, проверяем историю на делистинг
                h_test = ticker_obj.history(period="1d")
                if h_test is None or h_test.empty:
                    detected_type = "DELISTED"
                else:
                    detected_type = "UNDETERMINED"
                    
        except Exception as ticker_err:
            # Аварийный контур: перехватываем технические сбои структуры yfinance (вроде _dividends)
            # Раз yfinance споткнулся о внутренности живого чарта — бумага существует на мировых биржах!
            try:
                import pandas as pd
                h_test = ticker_obj.history(period="1d")
                if h_test is None or h_test.empty:
                    detected_type = "DELISTED"
                else:
                    last_date = h_test.index[-1]
                    if hasattr(last_date, 'to_pydatetime'):
                        last_date = last_date.to_pydatetime().replace(tzinfo=None)
                    
                    days_delta = (pd.Timestamp.now().replace(tzinfo=None) - last_date).days
                    if days_delta > 7:
                        detected_type = "DELISTED"
                    else:
                        detected_type = "UNDETERMINED"
            except Exception:
                # Если упало абсолютно всё, включая историю — значит бумаги реально нет в мире
                detected_type = "DELISTED"

        # Принудительно стандартизируем пустые маркеры под защитный дефолт
        if not detected_type or detected_type in ("NONE", "UNDEFINED"):
            detected_type = "UNDETERMINED"

        # 📝 ЗРЯЧИЙ UPDATE В СУБД С ПРИВЕДЕНИЕМ ВРЕМЕНИ К timestamp(0)
        sql_update_type = f"""
            UPDATE public.tickers 
            SET asset_type = '{detected_type}',
                last_updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
            WHERE id = {row_id};
        """
        try:
            db_sys.execute_query(sql_update_type)
            updated_count += 1
            
            # Красивое логирование результата разметки Universe
            if detected_type == "DELISTED":
                logging.info(f"   👉 Паспорт {uport_symbol:8} зафиксирован как: 💾 【 DELISTED 】")
            elif detected_type == "ETF":
                logging.info(f"   👉 Паспорт {uport_symbol:8} зафиксирован как: 🎯 【 ETF 】")
            else:
                logging.info(f"   👉 Паспорт {uport_symbol:8} зафиксирован как: 📝 【 {detected_type} 】")
        except Exception as db_update_err:
            logging.error(f"   ❌ Ошибка записи типа в СУБД для ID {row_id}: {db_update_err}")

    print("\n" + "="*110)
    print(f"🏁 [ГЕНЕРАЛЬНАЯ МИГРАЦИЯ ЗАВЕРШЕНА]: Успешно размечено: {updated_count} из {total_tickers} активов Universe.")
    print("="*110 + "\n")

if __name__ == "__main__":
    run_global_asset_type_migration()
