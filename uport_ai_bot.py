import os
import asyncio
from aiogram import Bot, Dispatcher, BaseMiddleware, types
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv
from pathlib import Path

# Импортируем готовые инстанции базы данных и мапперов из ядра
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers import strategy_resolver   #!!!!!!!!!!!!!!!!!!!!!!

# ЗАГРУЗКА ОКРУЖЕНИЯ
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Критическая ошибка: TELEGRAM_TOKEN отсутствует в .env")

# ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ИМПОРТ НАШИХ СЛЕДУЮЩИХ РОУТЕРОВ (Будем раскомментировать по мере сборки)
from bot_handlers import summary, portfolios, tickers, settings, watchlist, ticker_search, portfolio_admin, strategies, order_pipelines, digest, paper_execution

# РЕГИСТРАЦИЯ ГИРЛЯНДЫ МОДУЛЕЙ
# Диспетчер спускает сигналы сверху вниз по этой цепочке
dp.include_routers(
    summary.router,
    portfolios.router,
    tickers.router,
    settings.router,
    watchlist.router,
    ticker_search.router,
    strategy_resolver.router,
    portfolio_admin.router,
    strategies.router,
    order_pipelines.router,
    digest.router,
    paper_execution.router
)

async def main():
    print("🤖 [UPort Бот]: Запуск службы Telegram-интерфейса...")
    
    # Сносим вебхуки и чистим застрявшую очередь сообщений на серверах Дурова
    await bot.delete_webhook(drop_pending_updates=True)
    print("🧹 [UPort Бот]: Вебхуки очищены, очередь drop_pending_updates взведена.")
    
    # Запуск прослушивания сети
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 [UPort Бот]: Служба бота успешно остановлена.")
