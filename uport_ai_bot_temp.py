import asyncio
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, types, BaseMiddleware, F
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from dotenv import load_dotenv

# Импортируем компоненты инфраструктуры UPort
from database import db_bot, db_sys
from bot_handlers.common import MenuAction

from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.sync_account_fb import FreedomBrokerSyncManager

# Импортируем наш новый независимый аналитический модуль
from analytics.portfolio_auditor import generate_portfolio_passport, audit_ticker_for_portfolio

# Подключаем наши новые независимые модули-роутеры
from bot_handlers import backlog


logging.basicConfig(level=logging.INFO)

env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing in .env")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

sync_manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

# # --- УЛУЧШЕННАЯ ФАБРИКА CALLBACK DATA ---
# class MenuAction(CallbackData, prefix="uport"):
#     action: str              # Основной экшен навигации
#     portfolio_id: int = 0    # ID конкретного портфеля (0 - Сводный)
#     ticker_name: str = ""    # Имя тикера для карточки актива
#     sub_view: str = ""       # Под-вкладка для Экранов 2 и 3 ('assets', 'passport', 'owner', 'yahoo')

# Импортируем общую фабрику колбэков из файла-донора, исключая циклическое замыкание
from bot_handlers.common import MenuAction

# --- ANTI-FLOOD MIDDLEWARE ---
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 1.0):
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
                await event.answer("⏳ Секунду...", show_alert=False)
                return
            self.clones[key] = now
        return await handler(event, data)

dp.callback_query.middleware(ThrottlingMiddleware(latency=1.0))

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
    builder.row(types.InlineKeyboardButton(text="🛠️ Бэклог разработки", callback_data=MenuAction(action="backlog_main").pack()))
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
    for p in summary["portfolios"]:
        icon = "👤" if p["is_owner"] else "💼"
        builder.row(types.InlineKeyboardButton(text=f"{icon} {p['name']}", callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="").pack()))
    
    builder.row(types.InlineKeyboardButton(text="📦 Сводный портфель семьи", callback_data=MenuAction(action="view_portfolio", portfolio_id=0, sub_view="").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


# ─────────────────────────────────────────────────────────
# --- УРОВЕНЬ 2: ДЕТАЛИЗАЦИЯ ПОРТФЕЛЯ (ИНТЕРФЕЙС ШТОРОК) ---
# ─────────────────────────────────────────────────────────
@dp.callback_query(MenuAction.filter(F.action == "view_portfolio"))
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view  # 'assets' или 'passport' или "" (разводка)
    
    await callback.answer("Сборка аналитического паспорта...")

    # 1. Вызываем наш новый независимый модуль аудитора для сбора данных
    passport = await asyncio.to_thread(generate_portfolio_passport, p_id, db_bot)
    if not passport:
        await callback.message.edit_text("❌ Ошибка генерации паспорта портфеля.", reply_markup=get_back_to_menu_keyboard())
        return

    meta = passport["meta"]
    
    # Формируем жесткую базовую шапку экрана портфеля
    if p_id == 0:
        report_text = f"📦 **{meta['name']} (Без перегородок)**\n"
        cash_query = "SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM public.accounts;"
    else:
        report_text = f"📋 **Детали портфеля: {meta['name']}**\n"
        report_text += f"👤 Владелец: {meta['name']} | 🎯 Стратегия: `{meta['strategy']}`\n"
        cash_query = f"SELECT account_number, account_type, currency_id, cash_available, cash_reserved FROM public.accounts WHERE portfolio_id = {p_id};"

    # Подтягиваем наличный кэш
    cash_res = await execute_sql_async(cash_query)
    if not isinstance(cash_res, list):
        cash_res = [cash_res] if cash_res else []
    
    trade_cash_lines = []
    deposit_cash_lines = []
    for c in cash_res:
        available = float(c['cash_available'])
        reserved = float(c['cash_reserved'])
        free = available - reserved
        sign = CURRENCY_SIGNS.get(c['currency_id'], c['currency_id'])
        if c['account_type'] == 'trade' and (available != 0 or reserved != 0):
            trade_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f} (🔓 {sign}{free:,.2f} / 🔒 {sign}{reserved:,.2f})")
        elif c['account_type'] == 'deposit' and available != 0:
            deposit_cash_lines.append(f"   • {c['currency_id']}: {sign}{available:,.2f}")

    report_text += f"───────────────────\n"
    if trade_cash_lines:
        report_text += "💵 **Кэш на торговых счетах:**\n" + "\n".join(trade_cash_lines) + "\n"
    if deposit_cash_lines:
        report_text += "💰 **Накопительные D-счета:**\n" + "\n".join(deposit_cash_lines) + "\n"

    # Выводим блок Бронебойного Соответствия Стратегии (IPS)
    report_text += f"───────────────────\n"
    # ИСПРАВЛЕНИЕ ТЕКСТА: "Соответствие стратегии П10:"
    report_text += f"🛡️ **Соответствие стратегии {meta['name']}:**\n"
    if passport["violations"]:
        for v in passport["violations"]:
            report_text += f" {v}\n"
    else:
        report_text += " ✅ Все лимиты и налоговые риски портфеля соответствуют стратегии.\n"
    report_text += f"───────────────────\n"

    builder = InlineKeyboardBuilder()

    # Сетка переключателей режимов (Вкладки)
    builder.row(
        types.InlineKeyboardButton(text="📦 Состав портфеля", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()),
        types.InlineKeyboardButton(text="📊 Паспорт качества", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="passport").pack())
    )

    # ЛОГИКА РАЗВОДКИ ШТОРОК
    if view == "assets":
        report_text += "📦 **Текущий состав ценных бумаг:**"
        
        # Запрашиваем отсортированные тикеры из базы для формирования кнопок-строк
        if p_id == 0:
            assets_query = """
                SELECT t.full_ticker, SUM(a.quantity) as quantity, 
                       EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(MAX(a.position_opened_at), CURRENT_TIMESTAMP)))::int AS holding_days
                FROM public.assets a JOIN public.tickers t ON a.ticker_id = t.id
                WHERE a.quantity > 0 GROUP BY t.full_ticker, t.symbol, t.suffix ORDER BY t.symbol ASC, t.suffix ASC;
            """
        else:
            assets_query = f"""
                SELECT t.full_ticker, a.quantity, EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
                FROM public.assets a JOIN public.tickers t ON a.ticker_id = t.id
                WHERE a.portfolio_id = {p_id} AND a.quantity > 0 ORDER BY t.symbol ASC, t.suffix ASC;
            """
        assets_res_raw = await execute_sql_async(assets_query)
        assets_res = assets_res_raw if isinstance(assets_res_raw, list) else ([assets_res_raw] if assets_res_raw else [])

        if assets_res:
            for asset in assets_res:
                ticker = asset['full_ticker']
                qty = float(asset['quantity'])
                days = int(asset['holding_days'] or 0)
                
                # Добавляем широкую кнопку-строку во весь ряд
                builder.row(types.InlineKeyboardButton(
                    text=f"🔹 {ticker} | {qty:,.2f} шт. | {days}д.",
                    callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker, sub_view="owner").pack()
                ))
        else:
            report_text += "\n   *Ценные бумаги отсутствуют.*"

    elif view == "passport":
        avg = passport["averages"]
        report_text += (
            f"📊 **Паспорт качества портфеля (Yahoo):**\n"
            f" • Ср. окупаемость P/E: **{avg['pe_trailing']:.1f}** | Прогноз: **{avg['pe_forward']:.1f}**\n"
            f" • Коэффициент PEG: **{avg['peg_ratio']:.2f}**\n"
            f" • Цена к выручке P/S: **{avg['price_to_sales']:.1f}** | К балансу P/B: **{avg['price_to_book']:.1f}**\n"
            f" • Стоимость бизнеса EV/EBITDA: **{avg['ev_to_ebitda']:.1f}**\n"
            f" ───────────────────\n"
            f" • Долг к капиталу D/E: **{avg['debt_to_equity']:.1f}%**\n"
            f" • Ликвидность (Current Ratio): **{avg['current_ratio']:.2f}**\n"
            f" • Чистая маржинальность: **{avg['profit_margin']:.1f}%**\n"
            f" • Рентабельность ROE: **{avg['return_on_equity']:.1f}%**\n"
            f" ───────────────────\n"
            f" • Совокупные дивиденды: **{avg['dividend_yield']:.2f}%**\n"
            f" • Общий свободный кэш компаний: **${avg['free_cash_flow_m']:,.1f}M**\n"
        )
    else:
        report_text += "👉 *Нажмите кнопку «Состав портфеля» для списка бумаг или «Паспорт качества» для анализа финансовых мультипликаторов Yahoo.*"

    # Сервисный блок навигации
    builder.row(types.InlineKeyboardButton(text="🔙 К общей сводке", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass


# ─────────────────────────────────────────────────────────
# --- УРОВЕНЬ 3: АНАЛИТИЧЕСКАЯ КАРТОЧКА АКТИВА (ШТОРКИ) ---
# ─────────────────────────────────────────────────────────
@dp.callback_query(MenuAction.filter(F.action == "view_ticker"))
async def process_view_ticker(callback: types.CallbackQuery, callback_data: MenuAction):
    ticker_name = callback_data.ticker_name
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view # 'owner' или 'yahoo'
    
    await callback.answer(f"Анализ {ticker_name}...")

    # 1. Загружаем базовый контекст рынка, владения и ордеров из ядра
    context = await asyncio.to_thread(db_bot.get_ticker_context, ticker_name, p_id, callback.from_user.id)
    if not context:
        await callback.message.edit_text(f"❌ Ошибка: тикер `{ticker_name}` не найден.", reply_markup=get_back_to_menu_keyboard())
        return

    sign = CURRENCY_SIGNS.get(context["base_currency"], "$")

    # Сборка жесткой базовой шапки карточки тикера
    text = (
        f"📈 **Аналитическая карточка актива:** `{context['full_ticker']}`\n"
        f"🏢 Компания: **{context['company_name']}**\n"
        f"📡 Системный статус: `{context['tracking_status']}` | Добавил: *{context['added_by_user']}*\n"
        f"───────────────────\n"
        f"💵 Текущая цена рынка: **{sign}{context['last_price']:,.2f}**\n"
    )

    # 2. Вызываем Вторую функцию Контура 2 — Кросс-аудит лимитов конкретного счета
    if p_id > 0:
        ticker_warnings = await asyncio.to_thread(audit_ticker_for_portfolio, ticker_name, p_id, db_bot)
        # ИСПРАВЛЕНИЕ ТЕКСТА ШАПКИ: "Соответствие стратегии П10:"
        text += f"───────────────────\n🛡️ **Соответствие стратегии {context['portfolio_name'] if 'portfolio_name' in context else f'Портфеля #{p_id}'}:**\n"
        if ticker_warnings:
            for w in ticker_warnings:
                text += f" {w}\n"
        else:
            text += " ✅ Актив идеально соответствует лимитам и налоговой модели этого счета.\n"

    text += f"───────────────────\n"

    builder = InlineKeyboardBuilder()
    # ИСПРАВЛЕНИЕ ИНТЕРФЕЙСА: Меняем кнопки местами, Владение идет первым!
    builder.row(
        types.InlineKeyboardButton(text="💼 Владение и Ордера", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker_name, sub_view="owner").pack()),
        types.InlineKeyboardButton(text="📊 Метрики Yahoo", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker_name, sub_view="yahoo").pack())
    )

    # ЛОГИКА РАЗВОДКИ ШТОРОК ТИКЕРА
    if view == "yahoo":
        # Вытаскиваем сырые данные показателей Yahoo напрямую из public.tickers для этой вкладки
        t_raw = await execute_sql_async(f"SELECT * FROM public.tickers WHERE full_ticker = '{ticker_name}';")
        t = t_raw if isinstance(t_raw, dict) else (t_raw[0] if t_raw else {})
        
        if t.get('pe_trailing') is not None or t.get('debt_to_equity') is not None:
            fcf_val = float(t['free_cash_flow'] or 0) / 1_000_000 # Переводим FCF в миллионы
            text += (
                f"📊 **Фундаментальные показатели бизнеса:**\n"
                f" • Окупаемость P/E: **{float(t.get('pe_trailing') or 0):.1f}** | Прогноз P/E: **{float(t.get('pe_forward') or 0):.1f}**\n"
                f" • Коэффициент PEG: **{float(t.get('peg_ratio') or 0):.2f}**\n"
                f" • Цена к выручке P/S: **{float(t.get('price_to_sales') or 0):.1f}** | К балансу P/B: **{float(t.get('price_to_book') or 0):.1f}**\n"
                f" • Стоимость бизнеса EV/EBITDA: **{float(t.get('ev_to_ebitda') or 0):.1f}**\n"
                f" ───────────────────\n"
                f" • Долг к капиталу D/E: **{float(t.get('debt_to_equity') or 0):.1f}%**\n"
                f" • Ликвидность (Current Ratio): **{float(t.get('current_ratio') or 0):.2f}**\n"
                f" • Чистая маржинальность: **{float(t.get('profit_margin') or 0):.1f}%**\n"
                f" • Рентабельность ROE: **{float(t.get('return_on_equity') or 0):.1f}%**\n"
                f" ───────────────────\n"
                f" • Див. доходность: **{float(t.get('dividend_yield') or 0):.2f}%** | Выплаты (Payout): **{float(t.get('payout_ratio') or 0):.1f}%**\n"
                f" • Свободный кэш (FCF): **${fcf_val:,.1f}M**\n"
                f" ───────────────────\n"
                f" • Прогноз аналитиков (Target): **{sign}{float(t.get('target_mean_price') or 0):,.2f}**\n"
                f" • Рекомендация рынка (1-5): **{float(t.get('recommendation_mean') or 0):.1f}**\n"
            )
        else:
            text += "📊 **Фундаментальные показатели бизнеса:**\n   *Для данного типа актива (ETF/Фонд) экономические мультипликаторы стоимости отсутствуют.*"
    else:
        # Вкладка Владение и Ордера (По умолчанию)
        # --- МОДЕРНИЗИРОВАННЫЙ БЛОК ОТОБРАЖЕНИЯ БИРЖЕВЫХ ОРДЕРОВ ---
        text += "───────────────────\n⏳ **Живые биржевые приказы (Ордера):**\n"
        
        # Дополнительно вытаскиваем из СУБД тип ордера и текущую стоп-цену
        # Для этого перепишем локальный сбор ордеров прямо внутри хэндлера, чтобы не ломать get_ticker_context
        sql_live_orders = f"""
            SELECT p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, o.broker_order_id, o.currency_id
            FROM public.orders o
            JOIN public.portfolios p ON o.portfolio_id = p.id
            WHERE o.ticker_id = (SELECT id FROM public.tickers WHERE full_ticker = '{ticker_name}')
              AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
        """
        live_orders_res = await execute_sql_async(sql_live_orders)
        if not isinstance(live_orders_res, list):
            live_orders_res = [live_orders_res] if live_orders_res else []

        if live_orders_res:
            for ord_row in live_orders_res:
                o_type = int(ord_row['type'] or 2)
                oper = int(ord_row['oper'] or 3)
                qty = float(ord_row['q'])
                price_p = float(ord_row['p'] or 0)
                # Берем нашу новую стоп-цену, если она пустая — страхуемся старым полем
                stop_price = float(ord_row['stop_price'] if ord_row['stop_price'] is not None else price_p)
                
                o_sign = CURRENCY_SIGNS.get(ord_row['currency_id'], "$")
                p_name = ord_row['portfolio_name']
                o_id = ord_row['broker_order_id']

                # Интеллектуальный маппинг типов ордеров на основе спецификации API SDK
                if o_type == 5:
                    text += (f" • [{p_name}] 🛑 **СТОП-ЛОСС (По рынку)**:\n"
                             f"   👉 Количество: **{qty:.0f} шт.**\n"
                             f"   👉 Активация при цене: **{o_sign}{stop_price:,.2f}**\n"
                             f"   👉 ID ордера FB: `{o_id}`\n\n")
                elif o_type == 6:
                    text += (f" • [{p_name}] 📈 **ТЕМК-ПРОФИТ (По рынку)**:\n"
                             f"   👉 Количество: **{qty:.0f} шт.**\n"
                             f"   👉 Активация при цене: **{o_sign}{stop_price:,.2f}**\n"
                             f"   👉 ID ордера FB: `{o_id}`\n\n")
                else:
                    # Стандартные лимитные ордера (type=2)
                    op_label = "ПОКУПКА" if oper in (1, 2) else "ПРОДАЖА"
                    text += (f" • [{p_name}] 🔹 **ЛИМИТНАЯ {op_label}**:\n"
                             f"   👉 {qty:.0f} шт. по цене **{o_sign}{price_p:,.2f}**\n"
                             f"   👉 ID ордера FB: `{o_id}`\n\n")
        else:
            text += "   *Активных приказов по данной бумаге на бирже нет.*\n"


    # Кнопки навигации Уровня 3
    # Возвращаем пользователя на Экран 2 с сохранением той вкладки 'assets', чтобы клавиатура не слетала!
    builder.row(types.InlineKeyboardButton(text="🔙 К списку активов", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass


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

# 🌟 УНИВЕРСАЛЬНЫЙ АДМИН-МОСТ: Ловит ЛЮБЫЕ экшены бэклога на этаже @dp и шлет их в модуль backlog.py
@dp.callback_query(MenuAction.filter(F.action.startswith("backlog_")))
async def process_backlog_bridge(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    from bot_handlers.backlog import process_backlog_main, process_backlog_close, process_backlog_clear_done, process_backlog_add_mode
    
    act = callback_data.action
    
    # Распределяем сигналы по функциям нашего нового файла
    if act == "backlog_main":
        await process_backlog_main(callback, state)
    elif act == "backlog_close":
        await process_backlog_close(callback, callback_data, state)
    elif act == "backlog_clear_done":
        await process_backlog_clear_done(callback, state)
    elif act == "backlog_add_mode":
        await process_backlog_add_mode(callback, state)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    # Регистрируем модуль бэклога в глобальном диспетчере скелета
    dp.include_router(backlog.router)

    # Прокидываем db_bot как глобальную зависимость Aiogram во ВСЕ роутеры и хэндлеры
    dp["db_bot"] = db_bot

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
