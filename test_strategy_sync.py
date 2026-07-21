#!/usr/bin/env python3
import sys
import os
import logging

# Настраиваем вывод логов, чтобы видеть шаги работы нашего нового модуля
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Добавляем корень проекта в пути импорта Python
sys.path.append('/root/UPort')

# Импортируем готовый системный шлюз базы данных UPort
from database import db_sys
# Импортируем наш новый написанный модуль синхронизации стратегий
from brokers_connectors.sync_strategy_asset_fb import SyncStrategyAssetFB

def run_simulation():
    print("🧪 [UPORT ТЕСТЕР]: Запуск симуляции распределения стратегий...")
    
    # Инициализируем наш менеджер и передаем ему системную базу данных
    manager = SyncStrategyAssetFB(db_sys)
    
    # 🔒 ОТКРЫВАЕМ ЗАЩИЩЕННУЮ ТРАНЗАКЦИЮ (Все изменения внутри теста будут временными)
    db_sys.execute_query("BEGIN;")
    
    try:
        # --- ПОДГОТОВКА СТЕНДА: Ищем или создаем живые ID для теста ---
        # 1. Берем самый первый существующий портфель
        port_res = db_sys.execute_query("SELECT id FROM public.portfolios LIMIT 1;")
        if not port_res or len(port_res) == 0:
            print("❌ Ошибка: В базе данных нет ни одного портфеля для теста.")
            db_sys.execute_query("ROLLBACK;")
            return
        p_id = int(port_res[0]['id'])
        
        # 2. Берем самый первый существующий листинг и тикер
        list_res = db_sys.execute_query("SELECT id, ticker_id FROM public.listings LIMIT 1;")
        if not list_res or len(list_res) == 0:
            print("❌ Ошибка: В базе данных нет ни одного листинга в public.listings.")
            db_sys.execute_query("ROLLBACK;")
            return
        l_id = int(list_res[0]['id'])
        t_id = int(list_res[0]['ticker_id'])

        # 3. Находим ID стратегии "Нераспределенные" для этого портфеля
        unalloc_id = manager._get_unallocated_strategy_id(p_id)
        
        # 4. Создаем фейковую целевую стратегию (например, Трендовая №999) для чистоты теста
        #    template_id обязателен (NOT NULL) -- берём любой существующий шаблон (TREND_FOLLOWING).
        db_sys.execute_query(f"""
            INSERT INTO public.strategies (id, portfolio_id, template_id, strategy_name, strategy_share_pct)
            VALUES (999, {p_id}, (SELECT id FROM public.strategy_templates WHERE system_key = 'TREND_FOLLOWING'), 'Тестовая Трендовая', 20.00)
            ON CONFLICT (id) DO NOTHING;
        """)
        
        print(f"🎯 Стенд готов: Портфель={p_id}, Листинг={l_id}, Тикер={t_id}, Буферная Стратегия={unalloc_id}")
        print("-" * 60)

        # =====================================================================
        # 🔥 СЦЕНАРИЙ 1: Идеальное совпадение с утренним планом (Покупка 10 акций)
        # =====================================================================
        print("\n▶️ Тест 1: Плановая покупка. Создаем план в order_pipelines на +10 акций...")
        db_sys.execute_query(f"""
            INSERT INTO public.order_pipelines (portfolio_id, listing_id, ticker_id, strategy_id, target_quantity, pipeline_status)
            VALUES ({p_id}, {l_id}, {t_id}, 999, 10.0, 'PENDING');
        """)
        
        # Имитируем появление базового актива в общем котле СУБД (как это делает демон)
        db_sys.execute_query(f"""
            INSERT INTO public.assets (portfolio_id, listing_id, quantity, avg_price)
            VALUES ({p_id}, {l_id}, 10.0, 150.0)
            ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET quantity = 10.0;
        """)
        
        print("Имитируем, что в assets было 0 акций, а стало 10...")
        # Наш Метод 2 производит расчет дельты (+10) и распределение
        manager.distribute_asset_delta(portfolio_id=p_id, listing_id=l_id, ticker_id=t_id, old_qty=0.0, new_qty=10.0)
        
        # Проверяем, сколько акций попало в Тестовую Трендовую стратегию
        check_strat = db_sys.execute_query(f"SELECT allocated_quantity FROM public.strategy_assets WHERE strategy_id = 999;")
        print(f"📊 Результат в Тестовой стратегии: {check_strat}")
        
        # Проверяем, закрылся ли план
        check_pipe = db_sys.execute_query(f"SELECT pipeline_status FROM public.order_pipelines WHERE portfolio_id={p_id} AND ticker_id={t_id};")
        print(f"📊 Статус утреннего плана в базе: {check_pipe}")
        print("-" * 60)

        # =====================================================================
        # 🔥 СЦЕНАРИЙ 2: Внеплановая продажа руками (Уход буфера в отрицание)
        # =====================================================================
        print("\n▶️ Тест 2: Внеплановая продажа. Планов в базе нет. Продаем 5 акций...")
        print("Имитируем изменение в assets: было 10 акций, стало 5. Дельта = -5...")
        
        # Сначала очистим старые планы, чтобы тест шел вне плана
        db_sys.execute_query(f"DELETE FROM public.order_pipelines WHERE portfolio_id={p_id} AND ticker_id={t_id};")
        
        # Запускаем распределение для продажи
        manager.distribute_asset_delta(portfolio_id=p_id, listing_id=l_id, ticker_id=t_id, old_qty=10.0, new_qty=5.0)
        
        # Проверяем баланс в стратегии "Нераспределенные"
        check_unalloc = db_sys.execute_query(f"SELECT allocated_quantity FROM public.strategy_assets WHERE strategy_id = {unalloc_id};")
        print(f"📊 Результат бухгалтерского буфера (Нераспределенные): {check_unalloc}")
        print("-" * 60)

    except Exception as e:
        print(f"❌ Критическая ошибка в ходе теста: {e}")
    finally:
        # 🚨 ОТКАТЫВАЕМ ВСЕ ИЗМЕНЕНИЯ. База остается стерильно чистой!
        db_sys.execute_query("ROLLBACK;")
        print("\n🔒 [UPORT ТЕСТЕР]: Все тестовые данные успешно удалены из базы (ROLLBACK).")

if __name__ == "__main__":
    run_simulation()
