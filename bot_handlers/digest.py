import asyncio
import logging
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest

from database import db_sys
from bot_handlers.common import MenuAction
from analytics.daily_digest import assemble_portfolio_digest_data
from bot_handlers.bot_screens import render_digest_overview_text, render_digest_section_text
from bot_handlers.bot_keyboards import generate_digest_toc_keyboard, generate_digest_section_keyboard

router = Router()


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
