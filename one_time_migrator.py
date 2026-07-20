#!/usr/bin/env python3
import os
import sys
import logging

# Настройка логирования для вывода процесса на экран
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Добавляем корневую директорию проекта в пути поиска Python
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Импортируем готовый универсальный инстанс СУБД из вашей архитектуры
from database import db_sys

def run_migration():
    """
    🚚 РАЗОВЫЙ СКРИПТ ХОЛОДНОГО СТАРТА UPORT
    Очищает и заново наполняет таблицу strategy_assets, используя системный инстанс db_sys.
    """
    logging.info("🚀 Начало первичного распределения активов по стратегиям...")
    
    try:
        # 1. Очищаем таблицу перед наполнением
        db_sys.execute_query("TRUNCATE TABLE public.strategy_assets RESTART IDENTITY CASCADE;")
        logging.info("🧹 Таблица strategy_assets успешно очищена.")
        
        # 2. Вытаскиваем все физические активы от брокера
        sql_assets = "SELECT id, portfolio_id, quantity FROM public.assets WHERE quantity > 0;"
        all_assets = db_sys.execute_query(sql_assets) or []
        
        inserted_count = 0
        
        # 3. Распределяем каждую бумагу по утвержденной семейной логике
        for asset in all_assets:
            asset_id = int(asset["id"])
            portfolio_id = int(asset["portfolio_id"])
            qty = float(asset["quantity"])
            
            # Определяем целевую стратегию на основе портфеля
            if portfolio_id == 1:
                target_strategy_id = 1  # Револьверная стратегия
            elif portfolio_id == 2:
                target_strategy_id = 3  # Стратегия следования за трендом
            else:
                logging.warning(f"⚠️ Пропущен актив ID={asset_id}, неизвестный portfolio_id={portfolio_id}")
                continue
                
            # 4. Записываем зрячую виртуальную долю в СУБД
            sql_insert = f"""
                INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, expected_quantity)
                VALUES ({asset_id}, {target_strategy_id}, {qty}, 0.00);
            """
            db_sys.execute_query(sql_insert)
            inserted_count += 1
            
        logging.info(f"✅ Миграция завершена успешно. Распределено записей: {inserted_count}")
        
    except Exception as err:
        logging.error(f"❌ Сбой при наполнении strategy_assets: {err}")

if __name__ == "__main__":
    run_migration()
