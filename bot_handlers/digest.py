import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import db_sys
from bot_handlers.common import MenuAction
from analytics.daily_digest import assemble_portfolio_digest_data
from bot_handlers.bot_screens import render_digest_overview_text, render_digest_section_text
from bot_handlers.bot_keyboards import generate_digest_toc_keyboard, generate_digest_section_keyboard

router = Router()


@router.callback_query(MenuAction.filter(F.action == "show_digest_focus"))
async def process_digest_focus_menu(callback: types.CallbackQuery):
    """
    Ручной вызов дайджеста из главного меню (по просьбе пользователя 2026-07-30 --
    иначе дайджест доступен только по кнопкам самого пуш-уведомления, и просмотренное
    один раз сообщение позже некуда вернуться). Пикер портфелей -- тот же паттерн,
    что и у "🔬 Списки наблюдения" (bot_handlers/watchlist.py::process_watchlist_focus_menu).
    """
    await callback.answer("Загрузка дайджеста...")

    sql_portfolios = """
        SELECT p.id, p.name, u.name AS owner_name
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        ORDER BY p.id ASC;
    """
    p_res = await asyncio.to_thread(db_sys.execute_query, sql_portfolios)
    portfolios_list = p_res if isinstance(p_res, list) else ([p_res] if p_res else [])

    builder = InlineKeyboardBuilder()
    for p in portfolios_list:
        builder.row(types.InlineKeyboardButton(
            text=f"📅 Дайджест {p['name']} ({p['owner_name']})",
            callback_data=MenuAction(action="view_digest", portfolio_id=int(p["id"]), sub_view="overview").pack()
        ))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    try:
        await callback.message.edit_text("📅 **УТРЕННИЙ ДАЙДЖЕСТ**\nВыберите портфель:", parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "view_digest"))
async def process_view_digest(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Оглавление/разделы утреннего дайджеста (см. Claude/BACKLOG.md п.35). Данные
    пересчитываются заново на каждый клик -- снэпшот, не архив (см.
    Claude/11_asset_lifecycle_and_plan.md) -- поэтому дайджест трёхдневной давности
    остаётся кликабельным и покажет живые, а не протухшие цифры.
    """
    p_id = callback_data.portfolio_id
    section_key = callback_data.sub_view or "overview"
    await callback.answer()

    data = await asyncio.to_thread(assemble_portfolio_digest_data, db_sys, p_id)

    if section_key == "overview" or section_key not in data["sections"]:
        text = render_digest_overview_text(data)
        keyboard = generate_digest_toc_keyboard(p_id, data["sections"])
    else:
        text = render_digest_section_text(data, section_key)
        keyboard = generate_digest_section_keyboard(p_id, section_key, data["sections"][section_key]["items"])

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "digest_stub"))
async def process_digest_stub(callback: types.CallbackQuery, callback_data: MenuAction):
    """Заглушка для разделов дайджеста, у которых целевой экран ещё не решён (см. BACKLOG.md п.35)."""
    await callback.answer("🔧 Этот раздел ещё в разработке.", show_alert=True)
