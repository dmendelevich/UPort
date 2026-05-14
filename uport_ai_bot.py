import asyncio
import os
import logging
import subprocess
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path

# Импортируем компоненты инфраструктуры UPort
from database import Database
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.fb_sync_manager import FreedomBrokerSyncManager

logging.basicConfig(level=logging.INFO)

env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing in .env")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Инициализируем базы данных под две разные задачи
db_bot = Database(role="BOT")       
db_sys = Database(role="SYSTEM")    

# Создаем менеджер синхронизации
sync_manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

async def execute_sql_async(sql_query: str) -> list:
    return await asyncio.to_thread(db_bot.execute_query, sql_query)

async def run_repo_sync_async(account_number: str) -> dict:
    return await asyncio.to_thread(sync_manager.sync_by_account_number, account_number)

# Словарь валютных символов для красивого форматирования
CURRENCY_SIGNS = {
    "USD": "$",
    "EUR": "€",
    "RUR": "₽",
    "RUB": "₽",
    "KZT": "₸"
}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.",
        reply_markup=main_menu
    )

@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    sql = """
    SELECT p.name, COALESCE(SUM(a.quantity * a.avg_price), 0) as total 
    FROM portfolios p 
    LEFT JOIN assets a ON a.portfolio_id = p.id 
    GROUP BY p.name
    """
    data = await execute_sql_async(sql)
    if isinstance(data, list):
        text = "📊 **Текущий баланс базы данных:**\n\n"
        for item in data:
            text += f"🔹 **{item['name']}**: ${float(item['total']):,.2f}\n"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("❌ Ошибка при чтении баланса.")

# ИНТЕГРИРОВАННАЯ ЛОГИКА: Синхронизация по API + Очищенный Мультивалютный вывод
@dp.message(lambda message: message.text == "🚀 Начать")
async def process_init(message: types.Message):
    init_msg = await message.answer("�� **Связываюсь с Freedom Broker по API...**")

    # 1. Узнаем параметры счетов и часовой пояс пользователя из БД
    user_sql = f"SELECT id, name, timezone_offset FROM users WHERE telegram_id = {message.from_user.id}"
    user_data = await execute_sql_async(user_sql)
    
    if not user_data or len(user_data) == 0:
        await init_msg.edit_text("❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.")
        return
        
    user_id = user_data[0]['id']
    owner_name = user_data[0]['name']
    offset = user_data[0]['timezone_offset']

    # Находим все зарегистрированные номера счетов этого пользователя
    accounts_sql = f"SELECT DISTINCT account_number FROM accounts WHERE user_id = {user_id}"
    accounts_data = await execute_sql_async(accounts_sql)

    if not accounts_data:
        await init_msg.edit_text("❌ В таблице accounts не найдено счетов для вашего профиля.")
        return

    # 2. ЗАПУСКАЕМ ЖИВУЮ СИНХРОНИЗАЦИЮ ВСЕХ СЧЕТОВ ПО API БРОКЕРА (Торговые + Накопительные)
    for acc in accounts_data:
        acc_num = acc['account_number']
        try:
            await run_repo_sync_async(acc_num)
        except Exception as e:
            logging.error(f"Фоновая ошибка API для счета {acc_num}: {e}")

    # 3. СБОР И АНАЛИТИКА ОБНОВЛЕННЫХ ДАННЫХ ИЗ БАЗЫ
    await init_msg.edit_text("🔍 **Формирую мультивалютный аналитический отчет...**")

    # Получаем срез по акциям (активам) портфелей
    sql_portfolios = """
    SELECT 
        p.id as portfolio_id, p.name as portfolio_name,
        COUNT(a.ticker_id) as tickers_count, 
        COALESCE(SUM(a.quantity * a.avg_price), 0) as cost_basis,
        COALESCE(SUM(a.quantity * t.last_price), 0) as market_value,
        MAX(a.last_updated) as last_update
    FROM portfolios p
    LEFT JOIN assets a ON p.id = a.portfolio_id
    LEFT JOIN tickers t ON a.ticker_id = t.id
    WHERE p.owner_id = {user_id}
    GROUP BY p.id, p.name
    """.format(user_id=user_id)
    
    portfolios_res = await execute_sql_async(sql_portfolios)

    # Получаем срез по мультивалютному кэшу из таблицы accounts
    sql_cash = f"SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM accounts WHERE user_id = {user_id}"
    cash_res = await execute_sql_async(sql_cash)

    report_text = f"✅ **Аналитика UPort актуализирована по API!**\n"
    
    # Форматируем дату обновления
    display_date = "только что"
    if portfolios_res and isinstance(portfolios_res, list) and len(portfolios_res) > 0 and portfolios_res[0].get('last_update'):
        try:
            dt_str = str(portfolios_res[0]['last_update']).replace('T', ' ').split('.')[0]
            dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S') + timedelta(hours=offset)
            display_date = dt.strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            pass

    report_text += f"📈 **Состояние счетов на {display_date}:**\n\n"

    # ВЫВОД 1: ТОРГОВЫЕ ПОРТФЕЛИ (АКЦИИ + СВЯЗАННЫЙ КЭШ С БЛОКИРОВКАМИ)
    if portfolios_res and isinstance(portfolios_res, list):
        for p in portfolios_res:
            p_id = p['portfolio_id']
            basis = float(p['cost_basis'])
            market = float(p['market_value'])
            
            if market == 0 and basis > 0:
                market = basis
                
            profit = market - basis
            percent = (profit / basis * 100) if basis != 0 else 0
            icon = "📈" if profit >= 0 else "📉"
            
            report_text += (f"🔹 **Портфель {p['portfolio_name']}**: {p['tickers_count']} бум.\n"
                            f"   Закупка (API): **${basis:,.2f}**\n"
                            f"   Текущий рынок: **${market:,.2f}**\n"
                            f"   Результат: {icon} {profit:+,.2f} ({percent:+.2f}%)\n")
            
            if cash_res and isinstance(cash_res, list):
                cash_lines = []
                for c in cash_res:
                    available = float(c['cash_available'])
                    reserved = float(c['cash_reserved'])
                    free = available - reserved
                    sign = CURRENCY_SIGNS.get(c['currency_id'], c['currency_id'])

                    if available != 0 or reserved != 0:
                        # Для торговых счетов оставляем полную заблокированную/свободную структуру
                        if c['account_type'] == 'trade':
                            cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}\n"
                                             f"     🔒 Заблокировано: {sign}{reserved:,.2f}\n"
                                             f"     🔓 Свободно: {sign}{free:,.2f}")
                
                if cash_lines:
                    report_text += "   💵 **Кэш на счете:**\n" + "\n".join(cash_lines) + "\n"
            report_text += "\n"

    # ВЫВОД 2: НАКОПИТЕЛЬНЫЕ D-СЧЕТА (ЧИСТЫЙ ОЧИЩЕННЫЙ ВЫВОД БЕЗ БЛОКИРОВОК)
    if cash_res and isinstance(cash_res, list):
        deposit_lines = []
        for c in cash_res:
            if c['account_type'] == 'deposit':
                available = float(c['cash_available'])
                sign = CURRENCY_SIGNS.get(c['currency_id'], c['currency_id'])
                
                if available != 0:
                    # Убраны строки заблокировано/свободно для D-счета
                    deposit_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}")
        
        if deposit_lines:
            report_text += f"💰 **Накопительные D-счета ({owner_name}):**\n" + "\n".join(deposit_lines) + "\n\n"

    report_text += "Синхронизация завершена. Все мультивалютные кошельки учтены."
    await init_msg.edit_text(report_text, parse_mode="Markdown")

@dp.message(lambda message: message.text == "🔄 Обновить цены")
async def process_sync_prices(message: types.Message):
    wait_msg = await message.answer("⏳ Обновляю котировки с Yahoo Finance...")
    try:
        process = subprocess.Popen(['/root/UPort/venv/bin/python3', '/root/UPort/sync_prices.py'])
        await asyncio.to_thread(process.wait)
        await wait_msg.edit_text("✅ Цены в базе актуализированы!")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка выполнения скрипта цен: {e}")

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Начать")],
        [KeyboardButton(text="🔄 Обновить цены")]
    ],
    resize_keyboard=True
)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
