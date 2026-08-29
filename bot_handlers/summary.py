import asyncio
import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД и фабрику из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_main_menu_keyboard
from bot_handlers.bot_screens import format_capital_summary_text
from analytics.analytics_utils import convert_currency_amount

# Инициализируем изолированный роутер модуля
router = Router()


# --- ХЭНДЛЕРЫ УРОВНЯ 1 ---

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Ловит команду /start, проверяет админ-права и забирает числовой ID пользователя из СУБД, сохраняя в FSM."""
    await state.clear()
    
    is_admin = False
    user_db_id = None
    try:
        # 🔥 РЕФАКТОРИНГ: Заменяем на execute_row и полностью убираем проверку списков [0]
        user_row = db_bot.execute_row(
            "SELECT id, is_admin FROM public.users WHERE telegram_id = %s LIMIT 1;",
            (message.from_user.id,)
        )
        if user_row:
            # Извлекаем внутренний числовой ID пользователя напрямую из словаря
            if user_row.get('id') is not None:
                user_db_id = int(user_row['id'])
                
            # Поддерживаем разные типы ответов шлюза для админа
            is_admin = str(user_row.get('is_admin')).lower() in ('true', '1', 't')   

    except Exception as e:
        logging.error(f"⚠️ [Summary]: Ошибка определения прав и ID пользователя при старте: {e}")

    # 🔥 ЗАПИРАЕМ В ПАМЯТЬ: Сохраняем и флаг админа, и внутренний user_db_id текущей сессии
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.\n"
        f"Управляйте семейным капиталом через интерактивный пульт:",
        reply_markup=generate_main_menu_keyboard(is_admin=is_admin)
    )

@router.callback_query(MenuAction.filter(F.action == "main_menu"))
async def process_back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    """Возврат из любой шторки обратно в Главное меню (без потери user_db_id и без повторных запросов к БД)."""
    await callback.answer()
    
    # Извлекаем сохраненную роль и ID из памяти сессии
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    
    # Очищаем временные состояния шторки, но бережно возвращаем системные данные
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)
    
    try:
        await callback.message.edit_text(
            "📱 Главное меню системы UPort. Выберите действие:", 
            reply_markup=generate_main_menu_keyboard(is_admin=is_admin)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "show_summary"))
async def process_summary_callback(callback: types.CallbackQuery):
    """Экран Уровня 1: Выводит агрегированную сводку акций и мультивалютного кэша семьи."""
    await callback.answer("Запрашиваю сводку...")

    summary = await asyncio.to_thread(db_bot.get_family_summary, callback.from_user.id)
    if not summary:
        # Генерируем лаконичную одиночную кнопку Главного меню
        fallback_markup = generate_nav_back_keyboard(menu_only=True)
        await callback.message.edit_text(
            "❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", 
            reply_markup=fallback_markup
        )
        return
        
    text = format_capital_summary_text(summary, "📊 **Сводка семейного капитала**")

    builder = InlineKeyboardBuilder()

    # Группируем портфели по ВЛАДЕЛЬЦУ (не владелец+брокер, см. BACKLOG.md №108, 2026-08-14
    # -- у владельца может быть несколько брокеров сразу, экран "Счета" теперь показывает
    # ВСЕ его брокеры одним списком, отдельными секциями -- одна кнопка "Счета" на
    # владельца, не по одной на каждую пару владелец+брокер).
    groups = {}
    for p in summary["portfolios"]:
        groups.setdefault(p["owner_id"], []).append(p)

    for owner_id, group_portfolios in groups.items():
        row_buttons = []
        for p in group_portfolios:
            icon = "👤" if p["is_owner"] else "💼"
            row_buttons.append(types.InlineKeyboardButton(
                text=f"{icon} {p['name']}",
                callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="").pack()
            ))
        row_buttons.append(types.InlineKeyboardButton(
            text="🏦 Счета",
            callback_data=MenuAction(action="view_accounts", user_id=owner_id).pack()
        ))
        builder.row(*row_buttons)

    # Вытаскиваем накопленные в цикле кнопки срезов портфелей
    portfolios_markup = builder.as_markup()
    
    # Генерируем лаконичную одиночную кнопку Главного меню через универсальный навигационный пульт UPort
    reply_markup = generate_nav_back_keyboard(menu_only=True)
    
    # Ювелирно склеиваем кнопки портфелей и системный универсальный подвал
    final_builder = InlineKeyboardBuilder.from_markup(portfolios_markup)
    final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())


@router.callback_query(MenuAction.filter(F.action == "show_test_summary"))
async def process_test_summary_callback(callback: types.CallbackQuery):
    """
    Экран «Тестовый капитал» -- сводка ВИРТУАЛЬНЫХ портфелей (`broker_id IS NULL`,
    см. Claude/14_paper_portfolio.md; фильтр расширен с execution_mode='CONFIRM' 2026-08-29,
    когда появились «ПБумАвто»/«ПБумКлод» с другими режимами -- см. database.py::
    get_test_capital_summary), стандартизирован по образцу «Общей сводки
    капитала» (2026-08-03) -- общий текстовый блок (format_capital_summary_text),
    но без кнопки «Счета»: у бумажного портфеля один простой виртуальный счёт без
    деления на торговый/накопительный по разным валютам -- экран «Счета» здесь
    не имеет смысла, только кнопки портфелей.
    """
    await callback.answer("Запрашиваю тестовый капитал...")

    summary = await asyncio.to_thread(db_bot.get_test_capital_summary, callback.from_user.id)
    if not summary or not summary.get("portfolios"):
        fallback_markup = generate_nav_back_keyboard(menu_only=True)
        await callback.message.edit_text(
            "🧪 Тестовых портфелей пока нет.",
            reply_markup=fallback_markup
        )
        return

    text = format_capital_summary_text(summary, "🧪 **Тестовый капитал**")

    builder = InlineKeyboardBuilder()
    for p in summary["portfolios"]:
        icon = "👤" if p["is_owner"] else "💼"
        builder.row(types.InlineKeyboardButton(
            text=f"{icon} {p['name']}",
            # sub_view="assets/test_capital" -- составной sub_view с происхождением
            # (тот же приём, что уже применяется для шторки алертов, см. tickers.py),
            # чтобы карточка портфеля знала вести "назад" сюда, а не в реальную сводку.
            callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="assets/test_capital").pack()
        ))

    portfolios_markup = builder.as_markup()
    reply_markup = generate_nav_back_keyboard(menu_only=True)
    final_builder = InlineKeyboardBuilder.from_markup(portfolios_markup)
    final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())


@router.callback_query(MenuAction.filter(F.action == "view_accounts"))
async def process_view_accounts(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Экран "Счета": полная раскладка по счетам ОДНОГО владельца -- ВСЕ его брокеры,
    каждый отдельной секцией (BACKLOG.md №108, 2026-08-14 -- у владельца может быть
    несколько брокеров одновременно, раньше экран показывал ровно одного, выбранного
    кнопкой). Внутри каждой секции -- торговые счета всех его портфелей у этого
    брокера (бумаги + кэш) + накопительный счёт (принадлежит владельцу+брокеру, не
    одному портфелю -- см. обсуждение 2026-07-27).

    Развод "нативно / в вашей валюте": если внутри секции брокера ровно ОДНА валюта
    с ненулевым остатком (кэш+бумаги вместе) -- показываем нативный итог по брокеру,
    плюс отдельной строкой конвертированный, если он отличается от валюты смотрящего.
    Если валют несколько (живой пример -- накопительный П10, USD+RUR+KZT одновременно)
    -- нативного единого итога не существует физически, показываем только
    конвертированный. Курс -- везде в валюту СМОТРЯЩЕГО (callback.from_user.id), не
    владельца счетов -- та же схема, что у карточки портфеля/тикера/стратегии.
    """
    owner_id = callback_data.user_id
    await callback.answer("Собираю счета...")

    viewer_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT base_currency FROM public.users WHERE telegram_id = %s;", (callback.from_user.id,)
    )
    viewer_currency = (viewer_row or {}).get("base_currency") or "USD"

    currency_rows = await asyncio.to_thread(db_bot.execute_query, "SELECT id, sign FROM public.currencies;")
    currency_rows = currency_rows if isinstance(currency_rows, list) else ([currency_rows] if currency_rows else [])
    signs = {r["id"]: (r.get("sign") or r["id"]) for r in currency_rows}
    viewer_sign = signs.get(viewer_currency, viewer_currency)

    owner_row = await asyncio.to_thread(db_bot.execute_row, "SELECT name FROM public.users WHERE id = %s;", (owner_id,))
    owner_name = (owner_row or {}).get("name", "Unknown")

    accounts_rows = await asyncio.to_thread(
        db_bot.execute_query,
        """
            SELECT account_type, portfolio_id, portfolio_name, broker_id, broker_name,
                   broker_flag_emoji, currency_id, cash_available, cash_reserved
            FROM public.v_accounts_full
            WHERE user_id = %s AND broker_id IS NOT NULL
            ORDER BY broker_id, account_type DESC, portfolio_id, currency_id;
        """,
        (owner_id,)
    )
    accounts_rows = accounts_rows if isinstance(accounts_rows, list) else ([accounts_rows] if accounts_rows else [])

    text = f"🏦 **Счета — {owner_name}**\n"

    if not accounts_rows:
        text += "───────\nСчетов не найдено.\n"
        reply_markup = generate_nav_back_keyboard(
            one_step_back_text="🔙 К общей сводке",
            full_back_callback=MenuAction(action="show_summary").pack()
        )
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        except TelegramBadRequest:
            pass
        return

    # Группируем по брокеру, сохраняя порядок появления (ORDER BY broker_id выше).
    brokers_order = []
    by_broker = {}
    for a in accounts_rows:
        b_id = a["broker_id"]
        if b_id not in by_broker:
            by_broker[b_id] = {"name": a["broker_name"], "flag": a["broker_flag_emoji"] or "🏳️", "rows": []}
            brokers_order.append(b_id)
        by_broker[b_id]["rows"].append(a)

    grand_total_viewer = 0.0

    for b_id in brokers_order:
        broker = by_broker[b_id]
        text += f"\n{broker['flag']} **{broker['name']}**\n"

        trade_rows = [r for r in broker["rows"] if r["account_type"] == "trade"]
        deposit_rows = [r for r in broker["rows"] if r["account_type"] == "deposit"]

        # Портфели этой секции, сохраняя порядок появления.
        portfolios_order = []
        trade_by_portfolio = {}
        for r in trade_rows:
            p_id = r["portfolio_id"]
            if p_id not in trade_by_portfolio:
                trade_by_portfolio[p_id] = {"name": r["portfolio_name"] or f"Портфель #{p_id}", "rows": []}
                portfolios_order.append(p_id)
            trade_by_portfolio[p_id]["rows"].append(r)

        # Факты секции БЕЗ конвертации -- сумма по каждой встреченной валюте отдельно
        # (кэш торговых счетов + накопительный + рыночная стоимость бумаг вместе).
        section_amounts_by_currency = {}

        def add_amount(cur, amount):
            if amount == 0:
                return
            section_amounts_by_currency[cur] = section_amounts_by_currency.get(cur, 0.0) + amount

        # Рыночная стоимость бумаг каждого портфеля -- по нативной валюте листинга
        # (v_assets_full, Слой 1, см. Claude/02_universal_views.md), без конвертации.
        portfolio_stock_by_currency = {}
        for p_id in portfolios_order:
            asset_rows = await asyncio.to_thread(
                db_bot.execute_query,
                "SELECT quantity, listing_last_price, listing_currency_id FROM public.v_assets_full WHERE portfolio_id = %s;",
                (p_id,)
            )
            asset_rows = asset_rows if isinstance(asset_rows, list) else ([asset_rows] if asset_rows else [])
            stock_by_currency = {}
            for ar in asset_rows:
                cur = ar.get("listing_currency_id") or "USD"
                val = float(ar.get("quantity") or 0) * float(ar.get("listing_last_price") or 0)
                stock_by_currency[cur] = stock_by_currency.get(cur, 0.0) + val
            portfolio_stock_by_currency[p_id] = stock_by_currency
            for cur, val in stock_by_currency.items():
                add_amount(cur, val)

        for r in trade_rows:
            add_amount(r["currency_id"], float(r["cash_available"] or 0))
        for r in deposit_rows:
            add_amount(r["currency_id"], float(r["cash_available"] or 0))

        # --- Торговые счета ---
        if trade_by_portfolio:
            text += "💵 **Торговые счета:**\n"
            for p_id in portfolios_order:
                p_data = trade_by_portfolio[p_id]
                text += f"📦 {p_data['name']}:\n"
                for cur, val in portfolio_stock_by_currency.get(p_id, {}).items():
                    if val == 0:
                        continue
                    text += f"• Рыночная стоимость бумаг: {signs.get(cur, cur)}{val:,.2f}\n"
                for r in p_data["rows"]:
                    available = float(r["cash_available"] or 0)
                    reserved = float(r["cash_reserved"] or 0)
                    if available == 0 and reserved == 0:
                        continue
                    free = available - reserved
                    sign = signs.get(r["currency_id"], r["currency_id"])
                    text += f"• Кэш: {r['currency_id']} {sign}{available:,.2f} (🕊️ {sign}{free:,.2f} / 🔒 {sign}{reserved:,.2f})\n"
            text += "\n"

        # --- Накопительный счёт (без 🕊️/🔒 -- лимитных приказов там не бывает) ---
        if deposit_rows:
            text += "💰 **Накопительный счёт:**\n"
            for r in deposit_rows:
                available = float(r["cash_available"] or 0)
                if available == 0:
                    continue
                sign = signs.get(r["currency_id"], r["currency_id"])
                text += f"• {r['currency_id']}: {sign}{available:,.2f}\n"
            text += "\n"

        # --- Итого по брокеру ---
        distinct_currencies = list(section_amounts_by_currency.keys())
        if len(distinct_currencies) == 1:
            native_cur = distinct_currencies[0]
            native_total = section_amounts_by_currency[native_cur]
            text += f"Итого по брокеру: {signs.get(native_cur, native_cur)}{native_total:,.2f}\n"
            converted = await asyncio.to_thread(convert_currency_amount, db_bot, native_total, native_cur, viewer_currency)
            if native_cur != viewer_currency:
                text += f"В вашей валюте: {viewer_sign}{converted:,.2f}\n"
            grand_total_viewer += converted
        else:
            converted_total = 0.0
            for cur, amount in section_amounts_by_currency.items():
                converted_total += await asyncio.to_thread(convert_currency_amount, db_bot, amount, cur, viewer_currency)
            text += f"Итого по брокеру (в вашей валюте): {viewer_sign}{converted_total:,.2f}\n"
            grand_total_viewer += converted_total

        text += "───────\n"

    text += f"\n🎁 **ИТОГО:** {viewer_sign}{grand_total_viewer:,.2f}\n"

    reply_markup = generate_nav_back_keyboard(
        one_step_back_text="🔙 К общей сводке",
        full_back_callback=MenuAction(action="show_summary").pack()
    )

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass