#!/usr/bin/env python3
import os
import sys
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

# Подключаем пути ядра UPort
sys.path.append(str(Path(__file__).parent.resolve()))
from brokers_connectors.fb_client import FreedomBrokerClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def spy_on_freedom_broker():
    print("\n" + "="*100)
    logging.info("🕵️ [FB SPY]: Запуск точечного аудита полей API Freedom Broker для 'AAPL.US'...")
    print("="*100)

    # Загружаем ключи из .env
    env_path = Path(__file__).parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub, fb_priv = os.getenv("FB_DLM_API_KEY"), os.getenv("FB_DLM_API_SECRET")
    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)

    # Запрашиваем данные БЕЗ фильтрации, чтобы увидеть ВСЕ совпадения, если их несколько
    params = {
        "take": 10, "skip": 0,
        "filter": {"filters": [{"field": "ticker", "operator": "in", "value": "AAPL.US"}]}
    }

    try:
        raw_response = fb_client.execute(command="getAllSecurities", params=params)
        securities = raw_response.get("securities", []) if raw_response else []
        
        print(f"\n📊 Брокер вернул {len(securities)} объектов в массиве для 'AAPL.US'.\n")
        
        for idx, sec in enumerate(securities, start=1):
            print(f"--- 📦 ОБЪЕКТ №{idx} ---")
            print(f"  ▪️ Системный ID:    {sec.get('id')}")
            print(f"  ▪️ Тикер в ответе:   {sec.get('ticker')}")
            print(f"  ▪️ Торговая секция:  {sec.get('codesub_nm')} (Рынок: {sec.get('mkt_name')})")
            
            # Распаковываем блок quotes
            quotes_raw = sec.get("quotes", {})
            quotes = json.loads(quotes_raw) if isinstance(quotes_raw, str) else quotes_raw
            
            print("  📊 [ЦЕНОВЫЕ ПОЛЯ ИЗ QUOTES]:")
            print(f"    - Живая цена (ltp):         {quotes.get('ltp')}")
            print(f"    - Цена закрытия (pp):       {quotes.get('pp')}")
            print(f"    - Цена закрытия 2 (ClosePrice): {quotes.get('ClosePrice')}")
            print(f"    - Торговая сессия (ltr):    {quotes.get('ltr')}")
            print(f"    - Последняя сделка (ltt):   {quotes.get('ltt')}")
            print(f"    - Индикатор SMA-100 (p110): {quotes.get('p110')}")
            print(f"    - Индикатор SMA-200 (p220): {quotes.get('p220')}")
            print(f"    - Внебиржевой маркер (otc): {quotes.get('otc_instr')}")
            print("-" * 50)
            
    except Exception as e:
        logging.error(f"🚨 Ошибка шпионажа: {e}")

if __name__ == "__main__":
    spy_on_freedom_broker()
