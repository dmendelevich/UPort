#!/usr/bin/env python3
import sys
from pathlib import Path

# Подтягиваем системные пути ядра UPort
sys.path.append(str(Path(__file__).parent.resolve()))

from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.sync_quotes_fb import sync_quotes_fb_branch

def run_test_quotes_update():
    print("\n" + "="*95)
    print("📡 [ТЕСТЕР КОТИРОВОК FB]: Инициализация REST-обновителя цен...")
    print("="*95)

    # Чистый, зрячий запрос к листингам без лишних связей
    sql_get_test_data = """
        SELECT id AS listing_id, broker_symbol AS full_ticker
        FROM public.listings 
        WHERE broker_symbol IN ('BROS.US', 'XLV.US') AND broker_id = 1;
    """
    tickers_data = db_sys.execute_query(sql_get_test_data)

    if not tickers_data:
        print("❌ Ошибка: В листингах СУБД не найдены BROS.US или XLV.US. Заведите их сначала через ТГ-бот!")
        return

    print(f"📊 Сформирован симуляционный массив из СУБД: {tickers_data}")
    print("-" * 95)

    # Запускаем воркер, передавая очищенные данные
    sync_quotes_fb_branch(
        tickers_data=tickers_data,
        db_instance=db_sys,
        fb_client_class=FreedomBrokerClient
    )

    print("\n" + "="*95)
    print("🏁 [ТЕСТ ЗАВЕРШЕН]: Проверьте новые LTP-цены и UTC-секунды в public.listings!")
    print("="*95 + "\n")

if __name__ == "__main__":
    run_test_quotes_update()
