import asyncio
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, types, BaseMiddleware, F
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ReplyKeyboardRemove
from dotenv import load_dotenv

# Импортируем компоненты инфраструктуры UPort
from database import Database
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.sync_account_fb import FreedomBrokerSyncManager

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

# --- ФАБРИКА CALLBACK DATA ---
class MenuAction(CallbackData, prefix="uport"):
    action: str            # 'show_summary', 'main_menu', 'update_prices', 'view_portfolio'
    portfolio_id: int = 0  # ID конкретного портфеля для детализации

# --- ANTI-FLOOD MIDDLEWARE (Защита от дублирования нажатий) ---
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 2.0):
        self.latency = latency
        self.clones: Dict[str, float] = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, types.CallbackQuery):
            user_id = str(event.from_user.id)
            action = event.data or ""
            key = f"{user_id}:{action}"
            now = asyncio.get_event_loop().time()
            
            if key in self.clones and now - self.clones[key] < self.latency:
                await event.answer("⏳ Запрос обрабатывается, пожалуйста, подождите...", show_alert=False)
                return
            self.clones[key] = now
        return await handler(event, data)

dp.callback_query.middleware(ThrottlingMiddleware(latency=2.5))

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def execute_sql_async(sql_query: str) -> list:
    """Оставляем оригинальный метод для совместимости с Уровнем 2 детализации"""
    return await asyncio.to_thread(db_bot.execute_query, sql_query)

async def run_repo_sync_async(account_number: str) -> dict:
    return await asyncio.to_thread(sync_manager.sync_by_account_number, account_number)

CURRENCY_SIGNS = {
    "USD": "$", "EUR": "€", "RUR": "₽", "RUB": "₽", "KZT": "₸"
}

# --- ИНЛАЙН КЛАВИАТУРЫ ---
def get_main_menu_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="📊 Общая сводка капитала", 
        callback_data=MenuAction(action="show_summary").pack())
    )
    builder.row(types.InlineKeyboardButton(
        text="🔄 Обновить котировки бумаг и курсы валют", 
        callback_data=MenuAction(action="update_prices").pack())
    )
    return builder.as_markup()

def get_back_to_menu_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="📱 В главное меню", 
        callback_data=MenuAction(action="main_menu").pack())
    )
    return builder.as_markup()


# --- ХЭНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # ReplyKeyboardRemove() принудительно стирает старые "серые" кнопки с экрана пользователя
    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.\n"
        f"Управляйте семейным капиталом через интерактивный пульт:",
        reply_markup=get_main_menu_keyboard()
    )
    # Отправляем невидимый триггер удаления старых кнопок
    # await message.answer("🧹 Старый текстовый интерфейс очищен.", reply_markup=ReplyKeyboardRemove())

@dp.callback_query(MenuAction.filter(F.action == "main_menu"))
async def process_back_to_menu(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_text(
            "📱 Главное меню системы UPort. Выберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
    except TelegramBadRequest:
        pass


# --- УРОВЕНЬ 1: ОБЩАЯ СЕМЕЙНАЯ СВОДКА (ЧЕРЕЗ ЯДРО ДЕЛИГАТ) ---
@dp.callback_query(MenuAction.filter(F.action == "show_summary"))
async def process_summary_callback(callback: types.CallbackQuery):
    await callback.answer("Получаю агрегированные данные...")

    # Вызываем расчет сводки напрямую из класса Database ядра системы
    summary = await asyncio.to_thread(db_bot.get_family_summary, callback.from_user.id)
    
    if not summary:
        await callback.message.edit_text(
            "❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", 
            reply_markup=get_back_to_menu_keyboard()
        )
        return

    sign = summary["currency_sign"]
    assets = summary["total_assets"]
    cash = summary["total_cash"]
    base_curr = summary["base_currency"]

    text = (
        f"📊 **Сводка семейного капитала**\n"
        f"Расчет выполнен в вашей валюте: **{base_curr}**\n"
        f"───────────────────\n"
        f"📈 **Всего в акциях:** {sign}{assets:,.2f}\n"
        f"💵 **Доступный кэш:**  {sign}{cash:,.2f}\n"
        f"───────────────────\n"
        f"Выберите портфель ниже для просмотра деталей:"
    )

    # Строим кнопки на основе готового отсортированного списка портфелей ядра
    builder = InlineKeyboardBuilder()
    for p in summary["portfolios"]:
        icon = "👤" if p["is_owner"] else "💼"
        builder.row(types.InlineKeyboardButton(
            text=f"{icon} {p['name']}",
            callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id']).pack()
        ))
    
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


# --- УРОВЕНЬ 2: ДЕТАЛИЗАЦИЯ ПОРТФЕЛЯ ---
@dp.callback_query(MenuAction.filter(F.action == "view_portfolio"))
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    await callback.answer("Загрузка деталей...")
    p_id = callback_data.portfolio_id

    # Получаем базовую информацию о портфеле и владельце
    p_info_sql = f"""
        SELECT p.name as portfolio_name, u.name as owner_name, u.timezone_offset 
        FROM public.portfolios p
        LEFT JOIN public.users u ON p.owner_id = u.id
        WHERE p.id = {p_id};
    """
    p_info_raw = await execute_sql_async(p_info_sql)
    p_info = p_info_raw[0] if p_info_raw else None

    if not p_info:
        await callback.message.edit_text("❌ Портфель не найден.", reply_markup=get_back_to_menu_keyboard())
        return

    portfolio_name = p_info['portfolio_name']
    owner_name = p_info['owner_name'] or "Не указан"
    offset = p_info['timezone_offset'] or 0

    # Получаем срез по активам именно этого портфеля
    sql_assets = f"""
        SELECT 
            COUNT(a.ticker_id) as tickers_count, 
            COALESCE(SUM(a.quantity * a.avg_price), 0) as cost_basis,
            COALESCE(SUM(a.quantity * t.last_price), 0) as market_value,
            MAX(a.last_updated) as last_update
        FROM public.portfolios p
        LEFT JOIN public.assets a ON p.id = a.portfolio_id
        LEFT JOIN public.tickers t ON a.ticker_id = t.id
        WHERE p.id = {p_id}
        GROUP BY p.id, p.name;
    """
    assets_res_raw = await execute_sql_async(sql_assets)
    assets_res = assets_res_raw[0] if assets_res_raw else None

    # Получаем срез по мультивалютному кэшу из таблицы accounts для этого портфеля
    sql_cash = f"SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM public.accounts WHERE portfolio_id = {p_id};"
    cash_res = await execute_sql_async(sql_cash)

    display_date = "только что"
    if assets_res and assets_res.get('last_update'):
        try:
            dt_str = str(assets_res['last_update']).replace('T', ' ').split('.')[0]
            dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S') + timedelta(hours=offset)
            display_date = dt.strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            pass

    report_text = f"📋 **Детали портфеля: {portfolio_name}**\n"
    report_text += f"👤 Владелец: {owner_name}\n"
    report_text += f"🕒 Данные на: {display_date}\n"
    report_text += f"───────────────────\n\n"

    if assets_res:
        basis = float(assets_res['cost_basis'])
        market = float(assets_res['market_value'])
        tickers_count = assets_res['tickers_count']
        
        if market == 0 and basis > 0:
            market = basis
            
        profit = market - basis
        percent = (profit / basis * 100) if basis != 0 else 0
        icon = "📈" if profit >= 0 else "📉"
        
        report_text += (f"🔹 **Акции и фонды**: {tickers_count} бум.\n"
                        f"   Закупка (API): **${basis:,.2f}**\n"
                        f"   Текущий рынок: **${market:,.2f}**\n"
                        f"   Результат: {icon} {profit:+,.2f} ({percent:+.2f}%)\n\n")

    if cash_res and isinstance(cash_res, list):
        trade_cash_lines = []
        deposit_cash_lines = []
        
        for c in cash_res:
            available = float(c['cash_available'])
            reserved = float(c['cash_reserved'])
            free = available - reserved
            sign = CURRENCY_SIGNS.get(c['currency_id'], c['currency_id'])

            if c['account_type'] == 'trade' and (available != 0 or reserved != 0):
                trade_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}\n"
                                        f"     🔒 Заблокировано: {sign}{reserved:,.2f}\n"
                                        f"     🔓 Свободно: {sign}{free:,.2f}")
            elif c['account_type'] == 'deposit' and available != 0:
                deposit_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}")
        
        if trade_cash_lines:
            report_text += "💵 **Кэш на торговом счете:**\n" + "\n".join(trade_cash_lines) + "\n\n"
        
        if deposit_cash_lines:
            report_text += "💰 **Накопительные D-счета:**\n" + "\n".join(deposit_cash_lines) + "\n\n"

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 К общей сводке", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())


# --- ОБНОВЛЕНИЕ РЫНОЧНЫХ ЦЕН ПО КНОПКЕ ---
@dp.callback_query(MenuAction.filter(F.action == "update_prices"))
async def process_sync_prices_callback(callback: types.CallbackQuery):
    await callback.answer("Запуск обновления цен...")
    wait_msg = await callback.message.edit_text("⏳ **Актуализирую котировки акций и макрокурсы валют...**")
    try:
        import cron_scheduler
        
        await asyncio.to_thread(cron_scheduler.run_quotes_update, db_sys)
        await asyncio.to_thread(cron_scheduler.run_rates_update, db_sys)
        
        await wait_msg.edit_text(
            "✅ **Все цены акций и курсы макроконвертации в базе данных успешно обновлены напрямую через API!**", 
            parse_mode="Markdown", 
            reply_markup=get_back_to_menu_keyboard()
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка при принудительном обновлении цен: {e}", reply_markup=get_back_to_menu_keyboard())


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
