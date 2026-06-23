import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД и фабрику из доноров
from database import db_bot
from bot_handlers.common import MenuAction

# Инициализируем изолированный роутер модуля
router = Router()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КЛАВИАТУРЫ ---

async def execute_sql_async(sql_query: str) -> list:
    """Потокобезопасный асинхронный вызов шлюза СУБД."""
    return await asyncio.to_thread(db_bot.execute_query, sql_query)


def get_main_menu_keyboard(is_admin: bool = False) -> types.InlineKeyboardMarkup:
    """Генерирует пульт Главного меню на основе флага из памяти FSM."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="🔬 Списки наблюдения", callback_data=MenuAction(action="show_watchlist_focus").pack()))
    builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены рынка", callback_data=MenuAction(action="update_prices").pack()))
    builder.row(types.InlineKeyboardButton(text="🛠️ Бэклог разработки", callback_data=MenuAction(action="backlog_main").pack()))
    
    # ⚙️ Проверяем гибкий флаг админа. Кнопка доступна вам (и сыну в будущем)
    if is_admin:
        builder.row(types.InlineKeyboardButton(text="⚙️ Настройки системы", callback_data=MenuAction(action="settings_main").pack()))
        
    return builder.as_markup()


def get_back_to_menu_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопка возврата в меню."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    return builder.as_markup()


# --- ХЭНДЛЕРЫ УРОВНЯ 1 ---

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Ловит команду /start, один раз проверяет админ-права в БД и сохраняет в FSM."""
    await state.clear()
    
    is_admin = False
    try:
        # Ищем флаг is_admin напрямую по Telegram ID
        user_check = db_bot.execute_query(f"SELECT is_admin FROM public.users WHERE telegram_id = {message.from_user.id} LIMIT 1;")
        if user_check and isinstance(user_check, list) and len(user_check) > 0:
            user_row = user_check[0] if isinstance(user_check, list) else user_check
            # Поддерживаем разные типы ответов шлюза (bool или строку)
            is_admin = str(user_row.get('is_admin')).lower() in ('true', '1', 't')
    except Exception as e:
        print(f"⚠️ Ошибка определения админ-прав при старте: {e}")

    # Запираем флаг в память текущей сессии
    await state.update_data(is_admin=is_admin)

    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.\n"
        f"Управляйте семейным капиталом через интерактивный пульт:",
        reply_markup=get_main_menu_keyboard(is_admin=is_admin)
    )


@router.callback_query(MenuAction.filter(F.action == "main_menu"))
async def process_back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    """Возврат из любой шторки обратно в Главное меню (без повторных запросов к БД)."""
    await callback.answer()
    
    # Извлекаем сохраненную роль из памяти сессии
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    
    await state.clear()
    await state.update_data(is_admin=is_admin)
    
    try:
        await callback.message.edit_text(
            "📱 Главное меню системы UPort. Выберите действие:", 
            reply_markup=get_main_menu_keyboard(is_admin=is_admin)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "show_summary"))
async def process_summary_callback(callback: types.CallbackQuery):
    """Экран Уровня 1: Выводит агрегированную сводку акций и мультивалютного кэша семьи."""
    await callback.answer("Запрашиваю сводку...")

    summary = await asyncio.to_thread(db_bot.get_family_summary, callback.from_user.id)
    if not summary:
        await callback.message.edit_text(
            "❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", 
            reply_markup=get_back_to_menu_keyboard()
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
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
