import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД и фабрику из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_main_menu_keyboard

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
        print(f"⚠️ Ошибка определения прав и ID пользователя при старте: {e}")

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
        fallback_markup = generate_nav_back_keyboard(
            one_step_back_text="📱 В главное меню",
            full_back_callback=MenuAction(action="main_menu").pack()
        )
        await callback.message.edit_text(
            "❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", 
            reply_markup=fallback_markup
        )
        return
        
    sign = summary.get("currency_sign", "$")
    
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
        builder.row(types.InlineKeyboardButton(
            text=f"{icon} {p['name']}", 
            callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="").pack()
        ))
    
    builder.row(types.InlineKeyboardButton(text="📦 Сводный портфель семьи", callback_data=MenuAction(action="view_portfolio", portfolio_id=0, sub_view="").pack()))
    
    # Вытаскиваем накопленные в цикле кнопки срезов портфелей
    portfolios_markup = builder.as_markup()
    
    # Генерируем лаконичную одиночную кнопку Главного меню через универсальный навигационный пульт UPort
    reply_markup = generate_nav_back_keyboard(
        one_step_back_text="📱 В главное меню",
        full_back_callback=MenuAction(action="main_menu").pack()
    )
    
    # Ювелирно склеиваем кнопки портфелей и системный универсальный подвал
    final_builder = InlineKeyboardBuilder.from_markup(portfolios_markup)
    final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())