import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.summary import get_back_to_menu_keyboard, execute_sql_async

# Импортируем независимый аналитический модуль аудитора портфеля
from analytics.portfolio_auditor import generate_portfolio_passport

# Инициализируем локальный роутер для модуля портфелей
router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_portfolio"))
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    """Экран Уровня 2: Детализация конкретного счета семьи (Интерфейс шторок Состав / Паспорт)."""
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view  # 'assets', 'passport' или "" (разводка по умолчанию)
    
    print(f"\n💼 [ПОРТФЕЛЬ ТРИГГЕР]: Сборка аналитического паспорта для portfolio_id = {p_id}, вкладка = '{view}'")
    await callback.answer("Сборка аналитического паспорта...")

    # 1. Вызываем модуль аудитора для расчета рисков и соответствия стратегии
    passport = await asyncio.to_thread(generate_portfolio_passport, p_id, db_bot)
    if not passport:
        print(f"❌ [ПОРТФЕЛЬ ОШИБКА]: Не удалось сгенерировать паспорт для id = {p_id}")
        await callback.message.edit_text("❌ Ошибка генерации паспорта портфеля.", reply_markup=get_back_to_menu_keyboard())
        return

    meta = passport["meta"]
    
    # Формируем базовую текстовую шапку экрана портфеля
    if p_id == 0:
        report_text = f"📦 **{meta['name']} (Без перегородок)**\n"
        # 🔥 ДИНАМИЧЕСКИЙ JOIN ВАЛЮТ: Вытаскиваем значки знаков валют (sign) прямо из public.currencies!
        cash_query = """
            SELECT a.account_number, a.account_type, a.currency_id, a.cash_available, a.cash_reserved, cur.sign
            FROM public.accounts a
            JOIN public.currencies cur ON a.currency_id = cur.id;
        """
    else:
        report_text = f"📋 **Детали портфеля: {meta['name']}**\n"
        report_text += f"👤 Владелец: {meta['name']} | 🎯 Стратегия: `{meta['strategy']}`\n"
        # 🔥 ДИНАМИЧЕСКИЙ JOIN ВАЛЮТ: Вытаскиваем значки знаков валют (sign) прямо из public.currencies!
        cash_query = f"""
            SELECT a.account_number, a.account_type, a.currency_id, a.cash_available, a.cash_reserved, cur.sign
            FROM public.accounts a
            JOIN public.currencies cur ON a.currency_id = cur.id
            WHERE a.portfolio_id = {p_id};
        """

    print("🗄️ [ПОРТФЕЛЬ]: Запрашиваю мультивалютный кэш с динамическими значками из СУБД...")
    cash_res = await execute_sql_async(cash_query)
    if not isinstance(cash_res, list):
        cash_res = [cash_res] if cash_res else []
    
    trade_cash_lines = []
    deposit_cash_lines = []
    
    for c in cash_res:
        available = float(c['cash_available'])
        reserved = float(c['cash_reserved'])
        free = available - reserved
        # Знак валюты (sign) теперь честно и динамически прилетел из базы данных!
        o_sign = c['sign'] or c['currency_id']
        
        if c['account_type'] == 'trade' and (available != 0 or reserved != 0):
            trade_cash_lines.append(f"   • {c['currency_id']}: {o_sign}{available:,.2f} (🔓 {o_sign}{free:,.2f} / 🔒 {o_sign}{reserved:,.2f})")
        elif c['account_type'] == 'deposit' and available != 0:
            deposit_cash_lines.append(f"   • {c['currency_id']}: {o_sign}{available:,.2f}")

    report_text += f"───────────────────\n"
    if trade_cash_lines:
        report_text += "💵 **Кэш на торговых счетах:**\n" + "\n".join(trade_cash_lines) + "\n"
    if deposit_cash_lines:
        report_text += "💰 **Накопительные D-счета:**\n" + "\n".join(deposit_cash_lines) + "\n"

    # Выводим блок Бронебойного Соответствия Инвестиционной Декларации (IPS)
    report_text += f"───────────────────\n"
    report_text += f"🛡️ **Соответствие стратегии {meta['name']}:**\n"
    if passport["violations"]:
        for v in passport["violations"]:
            report_text += f" {v}\n"
    else:
        report_text += " ✅ Все лимиты и налоговые риски портфеля соответствуют стратегии.\n"
    report_text += f"───────────────────\n"

    builder = InlineKeyboardBuilder()

    # Сетка переключателей режимов (Вкладки-Шторки)
    builder.row(
        types.InlineKeyboardButton(text="📦 Состав портфеля", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()),
        types.InlineKeyboardButton(text="📊 Паспорт качества", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="passport").pack())
    )

    # ЛОГИКА ОТРЕНДЕРИВАНИЯ ВНУТРЕННОСТЕЙ ШТОРОК
    if view == "assets":
        report_text += "📦 **Текущий состав ценных бумаг:**"
        print("🔍 [ПОРТФЕЛЬ]: Извлекаю из СУБД список купленных активов на основе связки с listings...")
        
        if p_id == 0:
            # Сводный портфель всей семьи
            assets_query = """
                SELECT l.broker_symbol AS full_ticker, SUM(a.quantity) as quantity, 
                       EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(MAX(a.position_opened_at), CURRENT_TIMESTAMP)))::int AS holding_days
                FROM public.assets a 
                JOIN public.listings l ON a.listing_id = l.id
                WHERE a.quantity > 0 
                GROUP BY l.broker_symbol 
                ORDER BY l.broker_symbol ASC;
            """
        else:
            # Конкретный личный портфель члена семьи
            assets_query = f"""
                SELECT l.broker_symbol AS full_ticker, a.quantity, 
                       EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
                FROM public.assets a 
                JOIN public.listings l ON a.listing_id = l.id
                WHERE a.portfolio_id = {p_id} AND a.quantity > 0 
                ORDER BY l.broker_symbol ASC;
            """
        assets_res_raw = await execute_sql_async(assets_query)
        assets_res = assets_res_raw if isinstance(assets_res_raw, list) else ([assets_res_raw] if assets_res_raw else [])

        if assets_res:
            for asset in assets_res:
                ticker = asset['full_ticker']
                qty = float(asset['quantity'])
                days = int(asset['holding_days'] or 0)
                
                # Добавляем широкую интерактивную кнопку-строку во весь ряд
                builder.row(types.InlineKeyboardButton(
                    text=f"🔹 {ticker} | {qty:,.2f} шт. | {days}д.",
                    callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, ticker_name=ticker, sub_view="owner").pack()
                ))
        else:
            report_text += "\n   *Ценные бумаги в данном портфеле отсутствуют.*"

    elif view == "passport":
        print("📊 [ПОРТФЕЛЬ]: Рендеринг усредненного фундаментального паспорта Yahoo...")
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

    # Нижний сервисный блок навигации
    builder.row(types.InlineKeyboardButton(text="🔙 К общей сводке", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    print("🖥️ [ПОРТФЕЛЬ]: Отправляю собранную шторку в Telegram...")
    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass
