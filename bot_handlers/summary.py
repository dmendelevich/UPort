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

# Инициализируем изолированный роутер модуля
router = Router()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КЛАВИАТУРЫ ---

# async def execute_sql_async(sql_query: str) -> list:
#     """Потокобезопасный асинхронный вызов шлюза СУБД."""
#     return await asyncio.to_thread(db_bot.execute_query, sql_query)


# def get_main_menu_keyboard(is_admin: bool = False) -> types.InlineKeyboardMarkup:
#     """Генерирует пульт Главного меню на основе флага из памяти FSM."""
#     builder = InlineKeyboardBuilder()
#     builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
#     builder.row(types.InlineKeyboardButton(text="🔬 Списки наблюдения", callback_data=MenuAction(action="show_watchlist_focus").pack()))
#     builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены рынка", callback_data=MenuAction(action="update_prices").pack()))

#     # ⚙️ Проверяем гибкий флаг админа. Кнопка доступна вам (и сыну в будущем)
#     if is_admin:
#         builder.row(types.InlineKeyboardButton(text="⚙️ Настройки системы", callback_data=MenuAction(action="settings_main").pack()))
        
#     return builder.as_markup()


# def get_back_to_menu_keyboard() -> types.InlineKeyboardMarkup:
#     """Кнопка возврата в меню."""
#     builder = InlineKeyboardBuilder()
#     builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
#     return builder.as_markup()

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
            f"SELECT id, is_admin FROM public.users WHERE telegram_id = {message.from_user.id} LIMIT 1;"
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

    # Группируем портфели по (владелец, брокер) -- одна строка на пару, рядом с кнопками
    # портфелей группы отдельная кнопка "Счета" (накопительный счёт принадлежит владельцу
    # и брокеру, а не одному портфелю -- см. обсуждение 2026-07-27). Если у владельца
    # несколько портфелей у одного брокера, в строке будет несколько кнопок портфелей + одна "Счета".
    groups = {}
    for p in summary["portfolios"]:
        key = (p["owner_id"], p["broker_id"])
        groups.setdefault(key, []).append(p)

    for (owner_id, broker_id), group_portfolios in groups.items():
        row_buttons = []
        for p in group_portfolios:
            icon = "👤" if p["is_owner"] else "💼"
            row_buttons.append(types.InlineKeyboardButton(
                text=f"{icon} {p['name']}",
                callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="").pack()
            ))
        row_buttons.append(types.InlineKeyboardButton(
            text="🏦 Счета",
            callback_data=MenuAction(action="view_accounts", user_id=owner_id, broker_id=broker_id).pack()
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
    Экран «Тестовый капитал» -- сводка ВИРТУАЛЬНЫХ портфелей (execution_mode='CONFIRM',
    см. Claude/14_paper_portfolio.md), стандартизирован по образцу «Общей сводки
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
    """Экран "Счета": полная раскладка по счетам ОДНОГО владельца у ОДНОГО брокера --
    торговые счета всех его портфелей у этого брокера + накопительный счёт (см. обсуждение
    2026-07-27 -- накопительный счёт принадлежит владельцу+брокеру, а не одному портфелю,
    поэтому вынесен из карточки портфеля сюда)."""
    owner_id = callback_data.user_id
    broker_id = callback_data.broker_id

    await callback.answer("Собираю счета...")

    header_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT u.name AS owner_name, b.name AS broker_name
            FROM public.users u, public.brokers b
            WHERE u.id = {int(owner_id)} AND b.id = {int(broker_id)};
        """
    )
    owner_name = header_row.get("owner_name", "Unknown") if header_row else "Unknown"
    broker_name = header_row.get("broker_name", "Unknown") if header_row else "Unknown"

    accounts_rows = await asyncio.to_thread(
        db_bot.execute_query,
        f"""
            SELECT a.account_type, a.portfolio_id, p.name AS portfolio_name,
                   a.currency_id, cur.sign, a.cash_available, a.cash_reserved
            FROM public.accounts a
            JOIN public.currencies cur ON a.currency_id = cur.id
            LEFT JOIN public.portfolios p ON a.portfolio_id = p.id
            WHERE a.user_id = {int(owner_id)} AND a.broker_id = {int(broker_id)}
            ORDER BY a.account_type DESC, a.portfolio_id, a.currency_id;
        """
    )
    accounts_rows = accounts_rows if isinstance(accounts_rows, list) else ([accounts_rows] if accounts_rows else [])

    text = f"🏦 **Счета — {owner_name} ({broker_name})**\n───────\n"

    trade_by_portfolio = {}
    deposit_lines = []
    for a in accounts_rows:
        available = float(a['cash_available'])
        reserved = float(a['cash_reserved'])
        if available == 0 and reserved == 0:
            continue
        free = available - reserved
        sign = a['sign'] or a['currency_id'] or "$"
        line = f"• {a['currency_id']}: **{sign}{available:,.2f}** (🕊️ {sign}{free:,.2f} / 🔒 {sign}{reserved:,.2f})"
        if a['account_type'] == 'trade':
            p_name = a.get('portfolio_name') or f"Портфель #{a['portfolio_id']}"
            trade_by_portfolio.setdefault(p_name, []).append(line)
        else:
            deposit_lines.append(line)

    if trade_by_portfolio:
        text += "💵 **Торговые счета:**\n"
        for p_name, lines in trade_by_portfolio.items():
            text += f"📦 {p_name}:\n" + "\n".join(lines) + "\n"
        text += "\n"

    if deposit_lines:
        text += "💰 **Накопительный счёт:**\n" + "\n".join(deposit_lines) + "\n"

    if not trade_by_portfolio and not deposit_lines:
        text += "Счетов не найдено.\n"

    reply_markup = generate_nav_back_keyboard(
        one_step_back_text="🔙 К общей сводке",
        full_back_callback=MenuAction(action="show_summary").pack()
    )

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass