#!/usr/bin/env python3
import sys
import time
import logging
from pathlib import Path

# Подтягиваем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.resolve()))
sys.path.append(str(Path(__file__).parent / "site_connectors"))

from database import db_sys
import utils
from sync_fundamentals_yhoo import sync_fundamentals

# Настраиваем логирование прогресса
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_forced_global_fundamentals():
    print("\n" + "="*110)
    logging.info("🚀 [UPort MASS FUNDAMENTALS]: Старт принудительного генерального прогона Universe...")
    print("="*110)

    # 📡 ЗАПРОС НА ВЕСЬ ОЧИЩЕННЫЙ UNIVERSE, ГДЕ ФУНДАМЕНТАЛ ЕЩЕ ПУСТ ОПОСЛЯ ЧИСТКИ
    sql_get_empty = """
        SELECT symbol FROM public.tickers 
        WHERE fundamentals_last_synced_at IS NULL
          AND (provenance::text LIKE '%"MS_%' OR provenance::text LIKE '%LST_ID=%' OR provenance::text LIKE '%TG_USR_ID=%')
        ORDER BY symbol;
    """
    raw_rows = db_sys.execute_query(sql_get_empty)
    
    if not raw_rows or not isinstance(raw_rows, list):
        logging.info("ℹ️ Пустых или необновленных фундаментальных паспортов в СУБД не обнаружено. Прогон не требуется.")
        return

    all_symbols = [str(row["symbol"]).strip().upper() for row in raw_rows]
    total_tickers = len(all_symbols)
    logging.info(f"📊 Обнаружено {total_tickers} акций с пустым фундаменталом. Нарезаю на эшелоны...")

    # Нарезаем плоский список на эшелоны по 30 акций (получится примерно 31 эшелон)
    # Используем ваш родной и проверенный Chunker из utils
    echelons = list(utils.create_echelons(all_symbols, chunk_size=30))
    total_echelons = len(echelons)
    logging.info(f"📦 Сформировано {total_echelons} эшелонов для бережной загрузки.")

    processed_count = 0
    
    # 🔄 КА СКАДНЫЙ ПАКЕТНЫЙ КОНВЕЙЕР
    # 🔥 ИСПРАВЛЕНО: Явно распаковываем кортеж чанкера (номер пачки нам не нужен, берем только batch)
    for _, batch in echelons:
        # Теперь внутренний цикл будет бежать строго по текстовым буквам акций внутри текущей пачки
        for sym in batch:
            processed_count += 1
            logging.info(f"   👉 [{processed_count}/{total_tickers}] Отправка в одиночный боевой шлюз: '{sym}'")
            
            try:
                sync_fundamentals(db_sys, single_symbol=sym)
            except Exception as single_err:
                logging.error(f"   🚨 Сбой обработки фундаментала для '{sym}': {single_err}")

    print("\n" + "="*110)
    print(f"🏁 [ГЕНЕРАЛЬНЫЙ ПРОГОН ЗАВЕРШЕН]: Наполнено: {processed_count} из {total_tickers} фундаментальных профилей.")
    print("="*110 + "\n")

if __name__ == "__main__":
    run_forced_global_fundamentals()
