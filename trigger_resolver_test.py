#!/usr/bin/env python3
import sys
import os
import asyncio
import logging
from aiogram import Bot

# Настраиваем вывод логов
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Добавляем корень проекта в пути импорта
sys.path.append('/root/UPort')

# Импортируем шлюзы СУБД и утилиты UPort
from database import db_bot
from bot_handlers.bot_screens import format_premium_header
from bot_handlers.bot_keyboards import generate_target_strategies_keyboard

# 🔥 ЖЕСТКАЯ ИНСТРУКЦИЯ: Замените это число на ваш реальный числовой Telegram ID!
# (Например: 123456789)
TARGET_USER_ID = 250720161

async def run_trigger_test():
    print("🧪 [UPORT RESOLVER-ТЕСТЕР]: Запуск живой симуляции буфера...")
    
    # Инициализируем бота локально, забирая токен из окружения .env
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_TOKEN не найден в .env")
        return
        
    bot = Bot(token=token)

    try:
        # 1. Ищем в СУБД строго нашу тестовую позицию OPEN внутри Нераспределенных (ID=6) Портфеля №1
        sql = """
            SELECT a.listing_id, l.ticker_id, sa.allocated_quantity, sa.strategy_id
            FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            JOIN public.listings l ON a.listing_id = l.id
            WHERE sa.strategy_id = 6 
              AND a.portfolio_id = 1
              AND a.listing_id = 113
            LIMIT 1;
        """
        res = await asyncio.to_thread(db_bot.execute_query, sql)
        
        if not res or len(res) == 0:
            print("❌ Ошибка: В буферной стратегии 'Нераспределенные' (ID=6) сейчас пусто!")
            print("Убедитесь, что позиция по OPEN зафиксирована в public.strategy_assets.")
            return
            
        # 🔥 ХИРУРГИЧЕСКИЙ ФИКС РАСПАКОВКИ UPORT: Берем первый элемент списка [0]
        row = res[0] if isinstance(res, list) and len(res) > 0 else (res if isinstance(res, dict) else {})
        
        l_id = int(row['listing_id'])
        t_id = int(row['ticker_id'])
        qty = float(row['allocated_quantity'])
        src_strat_id = int(row['strategy_id'])

        print(f"🎯 Позиция найдена в СУБД: листинг {l_id}, тикер {t_id}, объем {qty} шт.")
        print(f"⏳ Отправляю интерактивный пульт на Telegram ID: {TARGET_USER_ID}...")

        # 2. Генерируем премиальный текст и пульт клавиатуры из наших утилит
        text_card = await format_premium_header(ticker_id=t_id, portfolio_id=1)
        text_msg = (
            f"{text_card}"
            f"⚠️ **ОБНАРУЖЕНА ВНЕПЛАНОВАЯ СДЕЛКА**\n"
            f"Зафиксировано движение объемом **{int(qty)} шт.**\n"
            f"Активы временно помещены в операционный буфер.\n\n"
            f"Пожалуйста, укажите целевую виртуальную стратегию:"
        )
        
        # Собираем пульт клавиатуры (Источник = src_strat_id, объем = qty)
        reply_markup = await generate_target_strategies_keyboard(
            portfolio_id=1, 
            ticker_id=t_id, 
            source_strategy_id=src_strat_id, 
            quantity=qty
        )

        # 3. Выталкиваем сообщение напрямую инвестору в телефон
        await bot.send_message(
            chat_id=TARGET_USER_ID, 
            text=text_msg, 
            parse_mode="Markdown", 
            reply_markup=reply_markup
        )
        print("🚀 Сообщение успешно доставлено! Проверьте ваш Телеграм-бот.")

    except Exception as err:
        print(f"❌ Критический сбой тестера: {err}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    if TARGET_USER_ID == "YOUR_TELEGRAM_USER_ID" or not isinstance(TARGET_USER_ID, int):
        print("🛑 ОСТАНОВЛЕНО: Пожалуйста, впишите ваш реальный числовой Telegram ID в переменную TARGET_USER_ID!")
    else:
        asyncio.run(run_trigger_test())
