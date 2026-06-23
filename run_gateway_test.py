#!/usr/bin/env python3
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Подгружаем пути, чтобы скрипт видел ядро UPort
sys.path.append(str(Path(__file__).parent.resolve()))
from database import db_sys
import utils
from brokers_connectors.fb_client import FreedomBrokerClient

if __name__ == "__main__":
    print("\n" + "=" * 110)
    print("🚀 [UPort GATEWAY LIVE TEST]: Проверка боевой функции в utils.py...")
    print("=" * 110 + "\n")

    # Инициализируем клиента Фридома
    env_path = Path(__file__).parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub = os.getenv("FB_DLM_API_KEY")
    fb_priv = os.getenv("FB_DLM_API_SECRET")

    if not fb_pub or not fb_priv:
        print("❌ Ошибка: Ключи API не найдены в .env файле!")
        sys.exit(1)

    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)

    # Тест 1: Новая американская акция (Полный цикл: создание + запись)
    id_meli = utils.ensure_ticker_passport_in_db(db_sys, "MELI", fb_client)
    print(f"   [РЕЗУЛЬТАТ MELI]: Получен system ticker_id = {id_meli}\n" + "-" * 110)

    # Тест 2: Повторный вызов для MELI (Проверка защиты от дубликатов ON CONFLICT)
    id_meli_dup = utils.ensure_ticker_passport_in_db(db_sys, "MELI", fb_client)
    print(f"   [РЕЗУЛЬТАТ MELI ДУБЛИКАТ]: Получен ticker_id = {id_meli_dup} (Должен совпасть с прошлым!)\n" + "-" * 110)

    # Тест 3: Канадская бумага HBM.TO (Проверка записи Канады и жесткого NULL для ФБ)
    id_hbm = utils.ensure_ticker_passport_in_db(db_sys, "HBM.TO", fb_client)
    print(f"   [РЕЗУЛЬТАТ HBM.TO]: Получен system ticker_id = {id_hbm}\n" + "=" * 110)
