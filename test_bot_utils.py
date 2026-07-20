#!/usr/bin/env python3
import sys
import os
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
sys.path.append('/root/UPort')

from database import db_sys, db_bot
from bot_handlers.bot_utils import format_premium_header, get_strategy_keyboard, execute_virtual_transfer

async def run_bot_simulation():
    print("🧪 [UPORT BOT-ТЕСТЕР]: Запуск симуляции интерфейса стратегий...")
    db_sys.execute_query("BEGIN;")
    
    try:
        p_id = 1
        t_id = 2209
        l_id = 112
        
        # 🔥 ЖЕСТКИЙ ФИКС СТЕНДА: Сносим остатки прошлых незавершенных тестов, 
        # чтобы гарантировать стерильность стартовых балансов (5.0 и 0.0)
        db_sys.execute_query(f"DELETE FROM public.strategy_assets WHERE strategy_id IN (999, 888);")
        db_sys.execute_query(f"DELETE FROM public.strategies WHERE id IN (999, 888);")
        
        db_sys.execute_query(f"INSERT INTO public.strategies (id, portfolio_id, strategy_name, strategy_share_pct) VALUES (999, {p_id}, 'Тестовый Источник', 10.00) ON CONFLICT (id) DO NOTHING;")
        db_sys.execute_query(f"INSERT INTO public.strategies (id, portfolio_id, strategy_name, strategy_share_pct) VALUES (888, {p_id}, 'Тестовый Приемник', 10.00) ON CONFLICT (id) DO NOTHING;")        
        
        # Исправленный INSERT листинга строго по вашей структуре DDL (без вымышленных колонок)
        db_sys.execute_query(f"""
            INSERT INTO public.listings (id, ticker_id, broker_id, broker_symbol, currency_id, last_price) 
            VALUES ({l_id}, {t_id}, 1, 'PLCE.US', 'USD', 3.04) 
            ON CONFLICT (id) DO NOTHING;
        """)
        
        print(f"🎯 Стенд инициализирован: PLCE (листинг {l_id}, тикер {t_id})")
        print("-" * 60)

        # 🧪 ТЕСТ 1: Шапка
        print("\n▶️ Тест 1: Генерация премиальной шапки текста...")
        text_preview = await format_premium_header(ticker_id=t_id, portfolio_id=p_id)
        print("👀 РЕНДЕРИНГ ЭКРАНА СМАРТФОНА:")
        print(text_preview)
        print("-" * 60)

        # 🧪 ТЕСТ 2: Кнопки
        print("\n▶️ Тест 2: Сборка Inline-клавиатуры для выбора приемника...")
        keyboard = await get_strategy_keyboard(portfolio_id=p_id, ticker_id=t_id, source_strategy_id=999, quantity=2.0)
        print(f"📊 Клавиатура собрана успешно! Объект клавиатуры получен.")
        print("-" * 60)

        # 🧪 ТЕСТ 3: Трансфер
        print("\n▶️ Тест 3: Проверка переноса долей 'Источник -> Приемник'...")
        db_sys.execute_query(f"INSERT INTO public.assets (portfolio_id, listing_id, quantity, avg_price) VALUES ({p_id}, {l_id}, 5.0, 3.04) ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET quantity = 5.0;")
        
        asset_res = db_bot.execute_query(f"SELECT id FROM public.assets WHERE portfolio_id={p_id} AND listing_id={l_id};")
        asset_row = asset_res[0] if isinstance(asset_res, list) and len(asset_res) > 0 else asset_res
        asset_id = int(asset_row['id'])

        db_sys.execute_query(f"INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity) VALUES ({asset_id}, 999, 5.0) ON CONFLICT DO NOTHING;")
        
        await execute_virtual_transfer(portfolio_id=p_id, listing_id=l_id, source_strategy_id=999, target_strategy_id=888, quantity=2.0)
        
        res_source = db_bot.execute_query(f"SELECT allocated_quantity FROM public.strategy_assets WHERE asset_id={asset_id} AND strategy_id=999;")
        res_target = db_bot.execute_query(f"SELECT allocated_quantity FROM public.strategy_assets WHERE asset_id={asset_id} AND strategy_id=888;")
        
        row_s = res_source[0] if isinstance(res_source, list) and len(res_source) > 0 else res_source
        row_t = res_target[0] if isinstance(res_target, list) and len(res_target) > 0 else res_target
        
        print(f"📊 Итог в Стратегии-Источнике (Ожидаем 3.0): {float(row_s['allocated_quantity'])}")
        print(f"📊 Итог в Стратегии-Приемнике (Ожидаем 2.0): {float(row_t['allocated_quantity'])}")
        print("-" * 60)

    except Exception as test_err:
        print(f"❌ Критическая ошибка при тестировании: {test_err}")
    finally:
        db_sys.execute_query("ROLLBACK;")
        print("\n🔒 [UPORT BOT-ТЕСТЕР]: Изменения успешно откатаны (ROLLBACK). База чиста.")

if __name__ == "__main__":
    asyncio.run(run_bot_simulation())
