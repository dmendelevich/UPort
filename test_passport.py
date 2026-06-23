#!/usr/bin/env python3
import sys
from pathlib import Path
import logging
import os
import json
from dotenv import load_dotenv

# Подключаем пути ядра UPort
sys.path.append(str(Path(__file__).parent.resolve()))
from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient

# 🔥 ИМПОРТИРУЕМ НАШУ ОБНОВЛЕННУЮ БОЕВУЮ ФУНКЦИЮ ИЗ UTILS.PY
from utils import ensure_ticker_passport_in_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_live_utils_test():
    # Загружаем ключи брокера из .env ядра
    env_path = Path(__file__).parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub, fb_priv = os.getenv("FB_DLM_API_KEY"), os.getenv("FB_DLM_API_SECRET")
    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)

    # Наш проверочный список из самых критических каверзных бумаг
    test_tickers = [
        "ULVR.L",       # Проверка легального суффикса Лондона
        "AAPL.US",       # Проверка срезки брокерской шелухи Америки
        "BRK.B.US.US",   # Проверка послойного снятия двойного повтора
        "CCBN.AIX.KZ",   # Проверка сложного Казахстана (секция Астаны AIX)
        "BP.L",          # Проверка создания уникальной лондонской строки
        "BP.US",         # Проверка создания уникальной американской строки (не затирая лондонскую!)
        "VUSA.EU"        # Проверка сложного европейского ETF (должен стать VUSA.L)
    ]
    
    print("\n🚀 Запуск ЖИВОГО ТЕСТА обновленного файла utils.py...")
    print("=" * 80)
    
    for ticker in test_tickers:
        # Вызываем боевую функцию — она проведет реальный цикл в utils и обновит СУБД
        ticker_id = ensure_ticker_passport_in_db(db_sys, ticker, fb_client)
        
        if not ticker_id:
            print(f"❌ ОШИБКА:utils.py вернул None для тикера {ticker}")
            
    print("=" * 80)
    print("🏁 Живой тест завершен! Выше должны напечататься строки 📌 [PASSPORT SUMMARY] по каждому тикеру.")

if __name__ == "__main__":
    run_live_test = run_live_utils_test
    run_live_test()
