#!/usr/bin/env python3
import os
import sys
import time
import logging
from pathlib import Path
from dotenv import load_dotenv
import yfinance as yf

# Подтягиваем системные пути, чтобы скрипт видел базу данных и ядро UPort
sys.path.append(str(Path(__file__).parent.resolve()))
from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient
from utils import ensure_ticker_passport_in_db

# Настраиваем логирование, чтобы видеть пакетный прогресс в реальном времени
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def create_echelons(data_list, chunk_size=50):
    """
    ВАШ РОДНОЙ И ОТЛАЖЕННЫЙ CHUNKER.
    Нарезает плоский список тикеров на изолированные пачки (эшелоны) по 50 штук,
    чтобы защитить API Freedom Broker от блокировок и ускорить пакетный сбор в Yahoo.
    """
    echelons = []
    for i in range(0, len(data_list), chunk_size):
        echelons.append(data_list[i:i + chunk_size])
    return echelons

def run_mass_passport_conveyer():
    logging.info("🚀 [UPort MASS RUNNER]: Старт генерального пакетного прогона Universe...")
    
    # Загружаем ключи брокера из .env файла ядра системы UPort
    env_path = Path(__file__).parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub, fb_priv = os.getenv("FB_DLM_API_KEY"), os.getenv("FB_DLM_API_SECRET")
    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)

    # 📡 ЗАПРОС НА ВЕСЬ ОЧИЩЕННЫЙ UNIVERSE ИЗ 940 ТИКЕРОВ
    sql_get_universe = "SELECT id, symbol FROM public.tickers ORDER BY symbol;"
    raw_universe_rows = db_sys.execute_query(sql_get_universe)
    
    if not raw_universe_rows or not isinstance(raw_universe_rows, list):
        logging.error("❌ База данных вернула пустой список Universe тикеров. Прогон невозможен.")
        return

    # Собираем чистый плоский список символов для нарезки
    all_symbols = [str(row["symbol"]).strip().upper() for row in raw_universe_rows]
    total_tickers = len(all_symbols)
    logging.info(f"📊 Из СУБД успешно загружено {total_tickers} акций. Запускаю нарезку на эшелоны...")

    # Нарезаем 940 тикеров на пачки по 50 штук (получится примерно 19 эшелонов)
    echelons = create_echelons(all_symbols, chunk_size=50)
    total_echelons = len(echelons)
    logging.info(f"📦 Universe успешно разделен на {total_echelons} эшелонов.")

    # 🚀 СТАРТ ПАКЕТНОГО КОНВЕЙЕРА (Проходим по эшелонам)
    processed_count = 0
    
    for idx, chunk in enumerate(echelons, start=1):
        logging.info(f"\n===== 📦 ОБРАБОТКА ЭШЕЛОНА {idx} ИЗ {total_echelons} (Пачка: {len(chunk)} акций) =====")
        
        # 🤝 СУПЕРЧАНК ДЛЯ YAHOO: Склеиваем 50 тикеров через пробел в одну строку
        # Из 'BP.L', 'AAPL', 'CCBN.QA' получается строка: 'BP.L AAPL CCBN.QA'
        yahoo_batch_string = " ".join(chunk)
        
        try:
            logging.info(f"   🌐 [YAHOO SUPERCHUNK]: Отправляю групповой сетевой запрос для {len(chunk)} бумаг...")
            # Yahoo за один сетевой запрос скачивает в память массив объектов для всей полусотни акций
            batch_tickers_obj = yf.Tickers(yahoo_batch_string)
            logging.info("   ✅ Пачка данных успешно загружена в оперативную память.")
        except Exception as batch_err:
            logging.error(f"   🚨 Ошибка пакетной загрузки Yahoo для эшелона {idx}: {batch_err}")
            # Если пачка упала целиком (сбой сети), не падаем, а идем к следующему эшелону
            continue

        # 🔄 ПОТОКОВАЯ ПРОПИСКА: Проходим по каждому тикеру внутри текущего эшелона
        for ticker in chunk:
            processed_count += 1
            logging.info(f"   👉 [{processed_count}/{total_tickers}] Подача на прописку: '{ticker}'")
            
            try:
                # Вызываем нашу боевую, отлаженную паспортистку v7.3 из utils.py
                # Она сделает каскадную очистку, сопоставит рынки ФБ через fb_markets,
                # заберет из памяти дату отчета и атомарно обновит строку в public.tickers
                ticker_id = ensure_ticker_passport_in_db(db_sys, ticker, fb_client)
                
                if not ticker_id:
                    logging.warning(f"   ⚠️ Паспортистка вернула None для тикера '{ticker}'")
            except Exception as single_err:
                logging.error(f"   🚨 Сбой обработки тикера '{ticker}': {single_err}")

        # ⏳ ТЕХНОЛОГИЧЕСКАЯ ПАУЗА МЕЖДУ ЭШЕЛОНАМИ (Защитный щит UPort)
        # Даем серверам Yahoo и Freedom Broker перевести дыхание перед следующей пачкой из 50 акций
        if idx < total_echelons:
            pause_time = 5 # 5 секунд паузы — идеальный баланс между скоростью и безопасностью Rate Limit
            logging.info(f"😴 [RATE LIMIT SHIELD]: Эшелон {idx} завершен. Спим {pause_time} секунд...")
            time.sleep(pause_time)

    print("\n" + "="*100)
    print(f"🏁 [ГЕНЕРАЛЬНЫЙ ПРОГОН ЗАВЕРШЕН]: Успешно обработано {processed_count} из {total_tickers} акций.")
    print("="*100 + "\n")

if __name__ == "__main__":
    # Запуск пакетного сборщика Universe
    run_mass_passport_conveyer()
