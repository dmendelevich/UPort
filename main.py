#!/usr/bin/env python3
import os
import sys
import asyncio
from dotenv import load_dotenv
from pathlib import Path

# 🔥 ЖЕЛЕЗНЫЙ АВТОМАТ ПУТЕЙ UPORT: Гарантирует корректный импорт модулей ядра при любом запуске
sys.path.append(str(Path(__file__).parent.resolve()))

# Импортируем готовый инстанс шлюза СУБД (лишний класс Database удален)
from database import db_sys

# Импортируем асинхронный сокет-демон Freedom Broker и реле времени
from brokers_connectors.fb_websocket_daemon import listen_freedom_broker
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.sync_account_fb import FreedomBrokerSyncManager

import cron_scheduler

# Загружаем переменные окружения
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

# Глобальная инициализация синк-менеджера
sync_manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

async def start_uport_system():
    # Подсказываем Python, чтобы он брал менеджер из глобального поля файла
    global sync_manager
    
    print("=== 🚀 [UPort Система] Глобальный запуск семейной экосистемы ===")

    # 1. Запрашиваем префиксы и базовые номера аккаунтов из таблицы users
    print("🧠 [Оркестратор]: Анализ конфигурации пользователей из таблицы users...")
    sql_get_users = "SELECT id, name, prefix, account_number FROM public.users;"
    
    try:
        active_users = db_sys.execute_query(sql_get_users)
    except Exception as db_fail:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА БД]: Не удалось получить список пользователей: {db_fail}")
        sys.exit(1)

    if not active_users:
        print("⚠️ [Оркестратор]: В таблице users нет зарегистрированных профилей. Старт невозможен.")
        return

    # Ленивая загрузка компонентов Telegram-бота
    print("🤖 [Оркестратор]: Ленивая загрузка компонентов Telegram-бота...")
    from uport_ai_bot import bot, dp

    async_tasks = []

    # 2. ЗАПУСКАЕМ НАШЕ РЕЛЕ ВРЕМЕНИ ФОНОВЫХ ЗАДАЧ (CRON)
    print("➕ [Оркестратор]: Регистрация реле времени фоновых задач cron_scheduler...")
    async_tasks.append(
        asyncio.create_task(cron_scheduler.start_clocks(db_sys, bot))
    )

    # 3. Динамически собираем потоки сокетов Freedom Broker на основе префиксов
    for row in active_users:
        u_id = row['id']
        u_name = row['name']
        prefix = row.get('prefix')
        base_account = row.get('account_number')

        # 🛡️ ПРАВКА №1: Защита от пустых профилей и служебных записей (типа AI)
        if not prefix or not base_account:
            print(f"ℹ️ [Оркестратор]: Профиль [{u_name}] (ID: {u_id}) пропущен — отсутствует префикс или номер счета.")
            continue
            
        prefix_upper = prefix.upper().strip()
        account_clean = str(base_account).strip()

        # 🛡️ ПРАВКА №2: Зрячая проверка ключей брокера в .env вместо капризного костыля "MDM"
        api_key_env = f"FB_{prefix_upper}_API_KEY"
        if not os.getenv(api_key_env):
            print(f"ℹ️ [Оркестратор]: Профиль [{u_name}] (ID: {u_id}, {prefix_upper}) передан под внешний контроль СУБД (нет ключей FB в .env).")
            continue
            
        # Контур Freedom Broker: ТОРГОВЫЙ СЧЕТ
        print(f"➕ [Оркестратор]: Регистрирую сокет Freedom Broker [{prefix_upper}] -> ТОРГОВЫЙ ({account_clean})")
        async_tasks.append(
            asyncio.create_task(listen_freedom_broker(prefix_upper, "trade", account_clean, sync_manager))
        )
        
        # Контур Freedom Broker: НАКОПИТЕЛЬНЫЙ СЧЕТ (автоматически добавляем префикс 'D')
        deposit_account = "D" + account_clean
        print(f"➕ [Оркестратор]: Регистрирую сокет Freedom Broker [{prefix_upper}] -> НАКОПИТЕЛЬНЫЙ ({deposit_account})")
        async_tasks.append(
            asyncio.create_task(listen_freedom_broker(prefix_upper, "deposit", deposit_account, sync_manager))
        )

    # 4. Добавляем в Event Loop запуск Telegram-бота
    print("➕ [Оркестратор]: Регистрирую службу Telegram-бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    async_tasks.append(
        asyncio.create_task(dp.start_polling(bot))
    )

    print("=== ✅ [Оркестратор]: Все компоненты увязаны в единый Event Loop. Старт потоков... ===\n")
    await asyncio.gather(*async_tasks)

if __name__ == "__main__":
    try:
        asyncio.run(start_uport_system())
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 [UPort Система]: Глобальный процесс успешно завершен. Все сокеты и боты закрыты.")
