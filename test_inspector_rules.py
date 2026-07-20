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
# Импортируем класс инспектора
from analytics.portfolio_inspector import PortfolioInspector

def run_rules_test():
    logging.info("🔬 [TEST]: Запуск комплексного аудита лимитов для Портфеля ID=2...")
    
    try:
        # 1. Инициализируем инспектора для вашего с женой портфеля (ID=2)
        inspector = PortfolioInspector(db_instance=db_sys, portfolio_id=2)
        
        # 2. Вызываем наш свежеиспеченный метод проверки лимитов и правил
        audit_result = inspector.audit_limits_and_rules()
        
        # 3. Красиво и наглядно выводим результат в формате JSON на экран
        print("\n" + "="*60)
        print("📊 ОТЧЕТ КОМПЛЕКСНОГО АУДИТА ЛИМИТОВ UPORT:")
        print("="*60)
        print(json.dumps(audit_result, indent=4, ensure_ascii=False))
        print("="*60 + "\n")
        
        if audit_result.get("has_violations"):
            logging.warning("🚨 Тест выявил нарушения лимитов в стратегиях портфеля!")
        else:
            logging.info("✅ Прекрасно! Ни одного нарушения лимитов или правил не обнаружено.")
            
    except Exception as err:
        logging.error(f"❌ Критический сбой во время проведения теста: {err}")

if __name__ == "__main__":
    run_rules_test()
