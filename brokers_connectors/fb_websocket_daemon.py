import os
import json
import asyncio
from dotenv import load_dotenv
from pathlib import Path

# Импортируем оригинальные классы установленного SDK
from tradernet import Core, TradernetWebsocket

# Путь к окружению
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

async def listen_freedom_broker(owner_prefix: str, account_type: str, account_number: str, sync_manager):
    """
    Фоновый асинхронный слушатель сокета Freedom Broker.
    Динамически выбирает торговые или накопительные ключи на основе account_type.
    Принимает строго определенный account_number для точечного вызова синхронизации.
    """
    if account_type == "deposit":
        api_key = os.getenv(f"FB_{owner_prefix}_D_API_KEY")
        api_secret = os.getenv(f"FB_{owner_prefix}_D_API_SECRET")
        mode_label = "НАКОПИТЕЛЬНЫЙ (D)"
    else:
        api_key = os.getenv(f"FB_{owner_prefix}_API_KEY")
        api_secret = os.getenv(f"FB_{owner_prefix}_API_SECRET")
        mode_label = "ТОРГОВЫЙ"

    # Если ключей в .env нет, мягко выходим (актуально для накопительного счета жены)
    if not api_key or not api_secret:
        print(f"ℹ️ [WS {owner_prefix}]: Ключи для контура {mode_label} отсутствуют в .env. Слушатель пропущен.")
        return

    print(f"🧠 [WS {owner_prefix} {mode_label}]: Инициализация ядра для счета {account_number}...")
    core_config = Core(public=api_key, private=api_secret)
    core_config.DOMAIN = "tradernet.kz"

    while True:
        try:
            print(f"🔌 [WS {owner_prefix} {mode_label}]: Подключение к {core_config.websocket_url}...")
            
            async with TradernetWebsocket(core_config) as ws_client:
                print(f"✅ [WS {owner_prefix} {mode_label}]: Соединение успешно установлено!")
                
                portfolio_stream = ws_client.portfolio()
                orders_stream = ws_client.orders()

                # Инициализируем асинхронный замок для защиты от Race Condition
                sync_lock = asyncio.Lock()

                async def read_portfolio():
                    async for update in portfolio_stream:
                        print(f"🟢 [WS {owner_prefix} {mode_label}]: Изменение портфеля! Вызов sync_manager...")
                        # Оборачиваем в замок: следующий поток не зайдет, пока текущий не завершит транзакцию в БД
                        async with sync_lock:
                            await asyncio.to_thread(sync_manager.sync_by_account_number, account_number)
                        print(f"🚀 [WS {owner_prefix} {mode_label}]: База данных актуализирована по портфелю.")

                async def read_orders():
                    async for update in orders_stream:
                        print(f"🔵 [WS {owner_prefix} {mode_label}]: ...")
                        # Используем тот же самый замок для синхронизации ордеров
                        async with sync_lock:
                            await asyncio.to_thread(sync_manager.sync_by_account_number, account_number)
                        print(f"🚀 [WS {owner_prefix} {mode_label}]: База данных актуализирована по ордерам.")

                await asyncio.gather(read_portfolio(), read_orders())

        except Exception as conn_error:
            print(f"⚠️ [WS {owner_prefix} {mode_label} DISCONNECT]: {conn_error}")
            print(f"⏳ [WS {owner_prefix} {mode_label}]: Переподключение через 10 секунд...")
            await asyncio.sleep(10)
