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
from brokers_connectors.fb_sync_manager import FreedomBrokerSyncManager

logging.basicConfig(level=logging.INFO)

env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing in .env")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Инициализируем базы данных
db_bot = Database(role="BOT")       
db_sys = Database(role="SYSTEM")    

# Создаем менеджер синхронизации
sync_manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

# --- УЛУЧШЕННАЯ ФАБРИКА CALLBACK DATA ---
class MenuAction(CallbackData, prefix="uport"):
    action: str              # 'show_summary', 'main_menu', 'update_prices', 'view_portfolio', 'view_ticker'
    portfolio_id: int = 0    # ID конкретного портфеля (0 - для Сводного портфеля семьи)
    ticker_name: str = ""    # Имя тикера для Экрана 3 (Карточка актива)

# --- ANTI-FLOOD MIDDLEWARE ---
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 1.5):
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
                await event.answer("⏳ Секунду, формирую экран...", show_alert=False)
                return
            self.clones[key] = now
        return await handler(event, data)

dp.callback_query.middleware(ThrottlingMiddleware(latency=1.5))

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def execute_sql_async(sql_query: str) -> list:
    return await asyncio.to_thread(db_bot.execute_query, sql_query)

CURRENCY_SIGNS = {
    "USD": "$", "EUR": "€", "RUR": "₽", "RUB": "₽", "KZT": "₸"
}

def get_main_menu_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены на рынке", callback_data=MenuAction(action="update_prices").pack()))
    return builder.as_markup()

def get_back_to_menu_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    return builder.as_markup()


# --- ХЭНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.\n"
        f"Управляйте семейным капиталом через интерактивный пульт:",
        reply_markup=get_main_menu_keyboard()
    )
    await message.answer("🧹 Старый текстовый интерфейс очищен.", reply_markup=ReplyKeyboardRemove())

@dp.callback_query(MenuAction.filter(F.action == "main_menu"))
async def process_back_to_menu(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_text("📱 Главное меню системы UPort. Выберите действие:", reply_markup=get_main_menu_keyboard())
    except TelegramBadRequest:
        pass


# ─────────────────────────────────────────────────────────
# --- УРОВЕНЬ 1: ОБЩАЯ СЕМЕЙНАЯ СВОДКА КАТЕГОРИЙ ---
# ─────────────────────────────────────────────────────────
@dp.callback_query(MenuAction.filter(F.action == "show_summary"))
async def process_summary_callback(callback: types.CallbackQuery):
    await callback.answer("Запрашиваю сводку...")

    summary = await asyncio.to_thread(db_bot.get_family_summary, callback.from_user.id)
    if not summary:
        await callback.message.edit_text("❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", reply_markup=get_back_to_menu_keyboard())
        return

    sign = summary["currency_sign"]
    text = (
        f"📊 **Сводка семейного капитала**\n"
        f"Расчет выполнен в вашей валюте: **{summary['base_currency']}**\n"
        f"───────────────────\n"
        f"📈 **Всего в акциях:** {sign}{summary['total_assets']:,.2f}\n"
        f"💵 **Доступный кэш:**  {sign}{summary['total_cash']:,.2f}\n"
        f"───────────────────\n"
        f"Выберите срез портфеля для детального анализа:"
    )

    builder = InlineKeyboardBuilder()
    # 1. Выводим список конкретных портфелей семьи (свои сверху)
    for p in summary["portfolios"]:
        icon = "👤" if p["is_owner"] else "💼"
        builder.row(types.InlineKeyboardButton(text=f"{icon} {p['name']}", callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id']).pack()))
    
    # 2. ИНТЕГРАЦИЯ: Добавляем четвертую архитектурную кнопку «Сводный портфель семьи»
    builder.row(types.InlineKeyboardButton(text="📦 Сводный портфель семьи", callback_data=MenuAction(action="view_portfolio", portfolio_id=0).pack()))
    
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


# ─────────────────────────────────────────────────────────
# --- УРОВЕНЬ 2: ДЕТАЛИЗАЦИЯ И ПОСТРОЧНЫЙ СОСТАВ ПОРТФЕЛЯ ---
# ─────────────────────────────────────────────────────────
@dp.callback_query(MenuAction.filter(F.action == "view_portfolio"))
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    await callback.answer("Сборка состава портфеля...")
    p_id = callback_data.portfolio_id

    # 1. Определяем запрашивающего пользователя для корректного пересчета валют на Лвл 2
    user_sql = f"SELECT id, timezone_offset FROM public.users WHERE telegram_id = {callback.from_user.id};"
    user_res = await execute_sql_async(user_sql)
    offset = user_res[0]['timezone_offset'] if user_res else 0

    # Шаблон текста
    if p_id == 0:
        report_text = "📦 **Сводный портфель всей семьи (Без перегородок)**\n"
        report_text += f"🕒 Данные консолидированы: только что\n"
        # Для сводного портфеля берем все активы без фильтра по ID
        assets_query = """
            SELECT t.full_ticker, t.company_name, SUM(a.quantity) as quantity, 
                   AVG(a.avg_price) as avg_price, MAX(a.position_opened_at) as position_opened_at
            FROM public.assets a
            JOIN public.tickers t ON a.ticker_id = t.id
            WHERE a.quantity > 0
            GROUP BY t.full_ticker, t.company_name;
        """
        cash_query = "SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM public.accounts;"
    else:
        # Для конкретного портфеля тянем его метаданные
        p_info_sql = f"SELECT p.name as portfolio_name, u.name as owner_name FROM public.portfolios p LEFT JOIN public.users u ON p.owner_id = u.id WHERE p.id = {p_id};"
        p_info = await execute_sql_async(p_info_sql)
        p_name = p_info[0]['portfolio_name'] if p_info else f"Портфель #{p_id}"
        o_name = p_info[0]['owner_name'] if p_info else "Не указан"
        
        report_text = f"📋 **Детали портфеля: {p_name}**\n"
        report_text += f"👤 Владелец: {o_name}\n"
        
        assets_query = f"""
            SELECT t.full_ticker, t.company_name, a.quantity, a.avg_price, a.position_opened_at, a.last_updated
            FROM public.assets a
            JOIN public.tickers t ON a.ticker_id = t.id
            WHERE a.portfolio_id = {p_id} AND a.quantity > 0;
        """
        cash_query = f"SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM public.accounts WHERE portfolio_id = {p_id};"

    # 2. Выполняем SQL-выборку ценных бумаг (Построчный список)
    assets_res = await execute_sql_async(assets_query)
    report_text += f"───────────────────\n\n📦 **Ценные бумаги на счетах:**\n"

    builder = InlineKeyboardBuilder()

    if assets_res:
        for idx, asset in enumerate(assets_res):
            ticker = asset['full_ticker']
            company = asset['company_name'] or "Акции"
            qty = float(asset['quantity'])
            avg_p = float(asset['avg_price'] or 0)
            
            # Считаем срок удержания (Holding Period)
            days = 0
            if asset.get('position_opened_at'):
                try:
                    opened_dt = datetime.strptime(str(asset['position_opened_at']).split('.')[0], '%Y-%m-%d %H:%M:%S')
                    days = (datetime.now() - opened_dt).days
                except Exception:
                    pass

            report_text += (f"🔹 **{ticker}** ({company})\n"
                            f"   • Количество: {qty:,.2f} шт.\n"
                            f"   • Срок удержания: **{days} дней**\n\n")
            
            # Генерируем инлайн-кнопку для этого актива (вставляем в сетку по 2 кнопки в ряд)
            builder.add(types.InlineKeyboardButton(text=f"🔍 {ticker}", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker).pack()))
    else:
        report_text += "   *В данном срезе ценные бумаги отсутствуют.*\n\n"

    # Выравниваем сетку кнопок активов по 2 штуки в ряд
    builder.adjust(2)

    # 3. Выводим кэш и накопительную часть
    cash_res = await execute_sql_async(cash_query)
    trade_cash_lines = []
    deposit_cash_lines = []
    
    if cash_res:
        for c in cash_res:
            available = float(c['cash_available'])
            reserved = float(c['cash_reserved'])
            free = available - reserved
            sign = CURRENCY_SIGNS.get(c['currency_id'], c['currency_id'])

            if c['account_type'] == 'trade' and (available != 0 or reserved != 0):
                trade_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f} (🔓 {sign}{free:,.2f} / 🔒 {sign}{reserved:,.2f})")
            elif c['account_type'] == 'deposit' and available != 0:
                deposit_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}")

    if trade_cash_lines:
        report_text += "💵 **Кэш на торговых счетах:**\n" + "\n".join(trade_cash_lines) + "\n\n"
    if deposit_cash_lines:
        report_text += "💰 **Накопительные D-счета:**\n" + "\n".join(deposit_cash_lines) + "\n\n"

    # Добавляем навигационные кнопки в самый конец под сеткой тикеров
    builder.row(types.InlineKeyboardButton(text="🔙 К общей сводке", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())


# ─────────────────────────────────────────────────────────
# --- УРОВЕНЬ 3: ИНТЕРАКТИВНАЯ КАРТОЧКА КОНКРЕТНОГО АКТИВА ---
# ─────────────────────────────────────────────────────────
@dp.callback_query(MenuAction.filter(F.action == "view_ticker"))
async def process_view_ticker(callback: types.CallbackQuery, callback_data: MenuAction):
    ticker_name = callback_data.ticker_name
    parent_portfolio_id = callback_data.portfolio_id  # Запоминаем, откуда пришли, для кнопки "Назад"
    
    await callback.answer(f"Анализ {ticker_name}...")

    # Вызываем наш универсальный метод ядра из database.py
    # Если запрашиваем из Сводного портфеля (p_id=0), передаем telegram_id=None для глобального семейного среза
    t_id = None if parent_portfolio_id == 0 else callback.from_user.id
    context = await asyncio.to_thread(db_bot.get_ticker_context, ticker_name, t_id)

    if not context:
        await callback.message.edit_text(f"❌ Ошибка: тикер {ticker_name} не найден в справочниках ядра.", reply_markup=get_back_to_menu_keyboard())
        return

    sign = context["currency_sign"] if "currency_sign" in context else CURRENCY_SIGNS.get(context["base_currency"], "$")
    
    text = (
        f"📈 **Аналитическая карточка актива: {context['full_ticker']}**\n"
        f"🏢 Компания: **{context['company_name']}**\n"
        f"───────────────────\n"
        f"💵 Рыночная цена: **{sign}{context['last_price']:,.2f}**\n"
        f"📡 Системный статус: `{context['tracking_status']}`\n"
        f"👤 Автор первого ввода: *{context['added_by_user']}*\n"
        f"───────────────────\n"
    )

    # БОК ВЛАДЕНИЯ (Построчный список из JSON от ядра)
    text += "💼 **Фактическое владение в семье:**\n"
    if context["ownership"]:
        for own in context["ownership"]:
            text += (f"• Портфель **{own['portfolio_name']}** ({own['owner_name']}):\n"
                     f"  👉 Количество: **{own['quantity']:.2f} шт.**\n"
                     f"  👉 Закупка (ср): {sign}{own['avg_price']:,.2f}\n"
                     f"  👉 Срок удержания: **{own['holding_days']} дней** (с {own['opened_date']})\n")
    else:
        text += "   *Ни один член семьи не держит этот актив на руках.*\n"

    text += "───────────────────\n"
    
    # БЛОК НАМЕРЕНИЙ (Лимитные приказы у брокера)
    text += "⏳ **Живые лимитные приказы (Ордера):**\n"
    if context["active_orders"]:
        for ord_row in context["active_orders"]:
            text += (f"• [{ord_row['portfolio_name']}] **{ord_row['operation']}**:\n"
                     f"  👉 {ord_row['quantity']:.0f} шт. по лимиту {sign}{ord_row['price']:,.2f}\n"
                     f"  👉 ID ордера FB: `{ord_row['broker_order_id']}`\n")
    else:
        text += "   *Активных приказов по данной бумаге на бирже нет.*\n"

    # Кнопки навигации Уровня 3
    builder = InlineKeyboardBuilder()
    # Кнопка возвращает строго на тот портфель (или сводный срез), с которого пользователь провалился в карточку
    builder.row(types.InlineKeyboardButton(text="🔙 К списку активов", callback_data=MenuAction(action="view_portfolio", portfolio_id=parent_portfolio_id).pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


# --- ОБНОВЛЕНИЕ РЫНОЧНЫХ ЦЕН ---
@dp.callback_query(MenuAction.filter(F.action == "update_prices"))
async def process_sync_prices_callback(callback: types.CallbackQuery):
    await callback.answer("Обновление котировок...")
    wait_msg = await callback.message.edit_text("⏳ **Актуализирую котировки акций и макрокурсы валют...**")
    try:
        import cron_scheduler
        await asyncio.to_thread(cron_scheduler.run_quotes_update, db_sys)
        await asyncio.to_thread(cron_scheduler.run_rates_update, db_sys)
        await wait_msg.edit_text("✅ **Все цены акций и курсы макроконвертации в базе данных успешно обновлены напрямую через API!**", parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())
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
