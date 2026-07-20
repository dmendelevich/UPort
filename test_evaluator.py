#!/usr/bin/env python3
import os
import sys
import logging
import json

# Настраиваем красивый вывод логов
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Добавляем корень проекта в пути поиска Python
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Импортируем наш универсальный системный инстанс СУБД
from database import db_sys
# Импортируем класс Эвалюатора из аналитических утилит
from analytics.analytics_utils import TickerEvaluator

def run_evaluator_test():
    logging.info("🔬 [TEST]: Запуск сквозного Эвалюатора бумаг UPort...")
    
    try:
        # 1. Инициализируем наш новый Эвалюатор
        evaluator = TickerEvaluator(db_instance=db_sys)
        
        # 2. Выбираем тестовый ticker_id из вашей базы данных.
        # Вытащим первый попавшийся актив из таблицы, например, с символом 'META' или 'MSFT'
        sql_find = "SELECT id, symbol FROM public.tickers WHERE symbol IN ('META', 'MSFT') LIMIT 1;"
        test_ticker = db_sys.execute_query(sql_find)
        
        if not test_ticker:
            logging.error("❌ В таблице public.tickers не найдено тестовых бумаг (META/MSFT/GLDM)!")
            return
            
        # Корректно извлекаем словарь, страхуясь от особенностей шлюзов СУБД
        ticker_row = test_ticker[0] if isinstance(test_ticker, list) and len(test_ticker) > 0 else test_ticker

        t_id = int(ticker_row["id"])
        symbol = ticker_row["symbol"]
        
        logging.info(f"🔍 [TEST]: Оцениваем тикер {symbol} (ticker_id={t_id}) для вашего портфеля ID=2...")
        
        # 3. Запускаем метод сквозной оценки
        report = evaluator.evaluate_ticker_strategy(ticker_id=t_id, target_portfolio_id=1)
        
        # 4. Красиво и наглядно выводим результат в формате JSON на экран
        print("\n" + "="*60)
        print(f"📊 ВЫХОДНОЙ PYTHON-СЛОВАРЬ ЭВАЛЮАТОРА ДЛЯ {symbol}:")
        print("="*60)
        print(json.dumps(report, indent=4, ensure_ascii=False))
        print("="*60 + "\n")
        
        logging.info("✅ Тест Эвалюатора успешно завершен!")
        
    except Exception as err:
        logging.error(f"❌ Критический сбой во время проведения теста: {err}")

if __name__ == "__main__":
    run_evaluator_test()
