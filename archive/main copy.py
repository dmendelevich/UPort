import os
import sys
import asyncio
from dotenv import load_dotenv
from pathlib import Path

# Импортируем компоненты шлюза СУБД
from database import Database

# Импортируем асинхронный сокет-демон Freedom Broker
from brokers_connectors.fb_websocket_daemon import listen_freedom_broker

# Загружаем переменные окружения
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

# Инициализируем системную роль для первоначальной сборки карты пользователей
db_sys = Database(role="SYSTEM")

async def trading212_cron_task(owner_prefix: str, account_number: str):
    """Встроенный асинхронный планировщик для Trading 212 (сын MDM)."""
    print(f"⏱️ [Cron {owner_prefix}]: Планировщик Trading 212 запущен для счета {account_number}.")
    while True:
        try:
            print(f"📡 [Cron {owner_prefix}]: Опрос API Trading 212 по расписанию...")
            # ТУТ В БУДУЩЕМ БУДЕТ ВЫЗОВ: t212_sync_manager.sync(account_number)
            await asyncio.sleep(900)
        except Exception as t212_err:
            print(f"❌ [Cron {owner_prefix} ERROR]: Ошибка опроса T212: {t212_err}")
            await asyncio.sleep(60)

async def start_uport_system():
    print("=== 🚀 [UPort Система] Глобальный запуск семейной экосистемы ===")

    # 1. Запрашиваем префиксы и базовые номера аккаунтов из таблицы users
    print("🧠 [Оркестратор]: Анализ конфигурации пользователей из таблицы users...")
    sql_get_users = "SELECT prefix, account_number FROM public.users;"
    
    try:
        active_users = db_sys.execute_query(sql_get_users)
    except Exception as db_fail:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА БД]: Не удалось получить список пользователей: {db_fail}")
        sys.exit(1)

    if not active_users:
        print("⚠️ [Оркестратор]: В таблице users нет зарегистрированных профилей.")
        return

    # ИСПРАВЛЕНО: Раскомментируем ленивый импорт бота. Теперь aiogram встанет в очередь Event Loop
    # строго ПОСЛЕ того, как все задачи сокетов будут порождены в памяти.
    print("🤖 [Оркестратор]: Ленивая загрузка компонентов Telegram-бота...")
    from uport_ai_bot import bot, dp, sync_manager

    async_tasks = []

    # 2. Динамически собираем потоки процессов на основе префиксов из СУБД
    for row in active_users:
        if not row.get('prefix') or not row.get('account_number'):
            continue
            
        prefix = row['prefix'].upper()
        base_account = str(row['account_number'])

        if prefix == "MDM":
            print(f"➕ [Оркестратор]: Регистрирую Cron-планировщик для Trading 212 [{prefix}]")
            async_tasks.append(
                asyncio.create_task(trading212_cron_task(prefix, base_account))
            )
        else:
            # Контур 1: ТОРГОВЫЙ СЧЕТ
            print(f"➕ [Оркестратор]: Регистрирую сокет Freedom Broker [{prefix}] -> Контур: ТОРГОВЫЙ ({base_account})")
            async_tasks.append(
                asyncio.create_task(listen_freedom_broker(prefix, "trade", base_account, sync_manager))
            )
            
            # Контур 2: НАКОПИТЕЛЬНЫЙ СЧЕТ (автоматически добавляем префикс 'D')
            deposit_account = "D" + base_account
            print(f"➕ [Оркестратор]: Регистрирую сокет Freedom Broker [{prefix}] -> Контур: НАКОПИТЕЛЬНЫЙ ({deposit_account})")
            async_tasks.append(
                asyncio.create_task(listen_freedom_broker(prefix, "deposit", deposit_account, sync_manager))
            )

    # 3. Добавляем в общий пул асинхронный запуск самого Telegram-бота
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
