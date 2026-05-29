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


def get_main_menu_keyboard() -> types.InlineKeyboardMarkup:
    """Генерирует пульт Главного меню."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены на рынке", callback_data=MenuAction(action="update_prices").pack()))
    # 🛠️ Наша админ-разминка: Кнопка бэклога, упакованная по единому стандарту фабрики
    builder.row(types.InlineKeyboardButton(text="🛠️ Бэклог разработки", callback_data=MenuAction(action="backlog_main").pack()))
    return builder.as_markup()


def get_back_to_menu_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопка возврата в меню."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    return builder.as_markup()


# --- ХЭНДЛЕРЫ УРОВНЯ 1 ---

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Ловит команду /start, сносит зависшие FSM и генерирует Главное меню."""
    await state.clear()
    await message.answer(
        f"Привет, {message.from_user.full_name}! Система UPort готова к работе.\n"
        f"Управляйте семейным капиталом через интерактивный пульт:",
        reply_markup=get_main_menu_keyboard()
    )


@router.callback_query(MenuAction.filter(F.action == "main_menu"))
async def process_back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    """Возврат из любой шторки обратно в Главное меню."""
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_text(
            "📱 Главное меню системы UPort. Выберите действие:", 
            reply_markup=get_main_menu_keyboard()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "show_summary"))
async def process_summary_callback(callback: types.CallbackQuery):
    """Экран Уровня 1: Выводит агрегированную сводку акций и мультивалютного кэша семьи."""
    await callback.answer("Запрашиваю сводку...")

    # Вызываем тяжелую функцию расчета семейной сводки из ядра базы
    summary = await asyncio.to_thread(db_bot.get_family_summary, callback.from_user.id)
    if not summary:
        await callback.message.edit_text(
            "❌ Ваш Telegram ID не зарегистрирован в базе данных UPort.", 
            reply_markup=get_back_to_menu_keyboard()
        )
        return

    # 🔥 ДИНАМИЧЕСКИЙ ЗНАЧОК: Берётся прямо из СУБД (из таблицы public.currencies, где мы исправили рубль!)
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
    
    # Динамически выстраиваем кнопки для личных портфелей членов семьи
    for p in summary["portfolios"]:
        icon = "👤" if p["is_owner"] else "💼"
        builder.row(types.InlineKeyboardButton(
            text=f"{icon} {p['name']}", 
            callback_data=MenuAction(action="view_portfolio", portfolio_id=p['id'], sub_view="").pack()
        ))
    
    # Кнопка Сводного портфеля всей семьи (portfolio_id = 0)
    builder.row(types.InlineKeyboardButton(text="📦 Сводный портфель семьи", callback_data=MenuAction(action="view_portfolio", portfolio_id=0, sub_view="").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
