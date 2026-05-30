import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.summary import get_back_to_menu_keyboard, execute_sql_async

# Импортируем наш аналитический модуль аудита тикеров
from analytics.portfolio_auditor import audit_ticker_for_portfolio

# Инициализируем локальный роутер для модуля карточек акций
router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_ticker"))
async def process_view_ticker(callback: types.CallbackQuery, callback_data: MenuAction):
    """Экран Уровня 3: Детализация конкретной бумаги (Шторки Владение / Метрики Yahoo)."""
    ticker_name = callback_data.ticker_name
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view # 'owner' или 'yahoo'
    
    print(f"\n📈 [ТИКЕР ТРИГГЕР]: Запрос карточки актива `{ticker_name}` для portfolio_id = {p_id}, шторка = '{view}'")
    await callback.answer(f"Анализ {ticker_name}...")

    # 1. Загружаем базовый контекст рынка, владения и ордеров из ядра базы
    context = await asyncio.to_thread(db_bot.get_ticker_context, ticker_name, p_id, callback.from_user.id)
    if not context:
        print(f"❌ [ТИКЕР ОШИБКА]: Тикер `{ticker_name}` не найден в контексте пользователя.")
        await callback.message.edit_text(f"❌ Ошибка: тикер `{ticker_name}` не найден.", reply_markup=get_back_to_menu_keyboard())
        return

    # 🔥 ДИНАМИЧЕСКИЙ МАКРОЗНАЧОК: Берём базовый графический знак валюты первичного листинга тикера прямо из базы
    sql_base_cur = f"SELECT sign FROM public.currencies WHERE id = '{context['base_currency']}';"
    cur_res = await execute_sql_async(sql_base_cur)
    sign = cur_res[0]['sign'] if cur_res and len(cur_res) > 0 else "$"

    # Сборка базовой шапки карточки тикера
    text = (
        f"📈 **Аналитическая карточка актива:** `{context['full_ticker']}`\n"
        f"🏢 Компания: **{context['company_name']}**\n"
        f"📡 Системный статус: `{context['tracking_status']}` | Добавил: *{context['added_by_user']}*\n"
        f"───────────────────\n"
        f"💵 Текущая цена рынка: **{sign}{context['last_price']:,.2f}**\n"
    )

    # 2. Вызываем кросс-аудит лимитов конкретного счета под инвестиционную стратегию (IPS)
    if p_id > 0:
        print(f"🛡️ [ТИКЕР]: Запускаю аудит соответствия лимитам стратегии портфеля #{p_id}...")
        ticker_warnings = await asyncio.to_thread(audit_ticker_for_portfolio, ticker_name, p_id, db_bot)
        text += f"───────────────────\n🛡️ **Соответствие стратегии {context['portfolio_name'] if 'portfolio_name' in context else f'Портфеля #{p_id}'}:**\n"
        if ticker_warnings:
            for w in ticker_warnings:
                text += f" {w}\n"
        else:
            text += " ✅ Актив идеально соответствует лимитам и налоговой модели этого счета.\n"

    text += f"───────────────────\n"

    builder = InlineKeyboardBuilder()
    
    # Сетка переключателей режимов карточки акции (Владение идет первым!)
    builder.row(
        types.InlineKeyboardButton(text="💼 Владение и Ордера", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker_name, sub_view="owner").pack()),
        types.InlineKeyboardButton(text="📊 Метрики Yahoo", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker_name, sub_view="yahoo").pack())
    )

    # ЛОГИКА РАЗВОДКИ ШТОРОК ТТИКЕРА
    if view == "yahoo":
        print("📊 [ТИКЕР]: Выгружаю фундаментальные коэффициенты из public.tickers...")
        t_raw = await execute_sql_async(f"SELECT * FROM public.tickers WHERE full_ticker = '{ticker_name}';")
        t = t_raw[0] if isinstance(t_raw, list) and len(t_raw) > 0 else (t_raw if isinstance(t_raw, dict) else {})
        
        if t and (t.get('pe_trailing') is not None or t.get('debt_to_equity') is not None):
            fcf_val = float(t['free_cash_flow'] or 0) / 1_000_000
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
        print("⏳ [ТИКЕР]: Выгружаю живые биржевые приказы из академической public.orders...")
        text += "───────────────────\n⏳ **Живые биржевые приказы (Ордера):**\n"
        
        # 🔥 АКАДЕМИЧЕСКИЙ ЗАПРОС v3.0: Извлекаем значки валют приказов напрямую из listings -> currencies
        sql_live_orders = f"""
            SELECT p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, o.broker_order_id, cur.sign, l.broker_symbol
            FROM public.orders o
            JOIN public.listings l ON o.listing_id = l.id
            JOIN public.portfolios p ON o.portfolio_id = p.id
            JOIN public.currencies cur ON l.currency_id = cur.id
            WHERE l.broker_symbol = '{ticker_name.strip().upper()}'
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
                stop_price = float(ord_row['stop_price'] if ord_row['stop_price'] is not None else price_p)
                
                # Значок валюты ордера динамически прилетает из СУБД на основе листинга!
                o_sign = ord_row['sign'] or "$"
                p_name = ord_row['portfolio_name']
                o_id = ord_row['broker_order_id']
                
                # Интеллектуальный маппинг типов ордеров на основе спецификации API Tradernet SDK
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
                    op_label = "ПОКУПКА" if oper in (1, 2) else "ПРОДАЖА"
                    text += (f" • [{p_name}] 🔹 **ЛИМИТНАЯ {op_label}**:\n"
                             f"   👉 {qty:.0f} шт. по цене **{o_sign}{price_p:,.2f}**\n"
                             f"   👉 ID ордера FB: `{o_id}`\n\n")
        else:
            text += "   *Активных приказов по данной бумаге на бирже нет.*\n"


    # Нижняя навигация: возвращает на Экран 2 (к составу портфеля) без слёта шторок!
    builder.row(types.InlineKeyboardButton(text="🔙 К списку активов", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    print("🖥️ [ТИКЕР]: Отправляю карточку акции в Telegram...")
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass
