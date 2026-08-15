import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from database import db_sys, db_bot
from bot_handlers.common import MenuAction
from analytics.daily_digest import assemble_portfolio_digest_data, filter_digest_data_by_strategy
from bot_handlers.bot_screens import (
    render_digest_overview_text, render_digest_section_text,
    format_portfolio_header, format_strategy_header, format_strategy_capital_block,
)
from bot_handlers.bot_keyboards import generate_digest_toc_keyboard, generate_digest_section_keyboard, generate_nav_back_keyboard
from analytics.analytics_utils import convert_currency_amount

router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_digest"))
async def process_view_digest(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Оглавление/разделы утреннего дайджеста (см. Claude/BACKLOG.md п.35). Данные
    пересчитываются заново на каждый клик -- снэпшот, не архив (см.
    Claude/11_asset_lifecycle_and_plan.md) -- поэтому дайджест трёхдневной давности
    остаётся кликабельным и покажет живые, а не протухшие цифры.

    Вызывается как вкладка «📅 Дайджест» с карточки портфеля (bot_handlers/portfolios.py)
    и карточки стратегии (bot_handlers/strategies.py) -- тема «дайджест как вкладка»,
    2026-08-14/15, заменила отдельный пикер портфелей из главного меню. strategy_id != 0 --
    сузить до одной стратегии (filter_digest_data_by_strategy); portfolio_id всегда
    известен -- считаем полный дайджест портфеля один раз, фильтр по стратегии --
    только презентационный, поверх уже готовых данных.

    Хедер -- ТОТ ЖЕ САМЫЙ стандартный хедер, что и у остальных вкладок этого экрана
    (format_portfolio_header / format_strategy_header+format_strategy_capital_block,
    2026-08-15) -- вкладка «Дайджест» не должна визуально отличаться от Состав/Паспорт/
    Стратегии. render_digest_overview_text/render_digest_section_text вызываются с
    standalone=False -- своё имя/капитал не повторяют то, что уже в хедере.
    """
    p_id = callback_data.portfolio_id
    s_id = callback_data.strategy_id or 0
    section_key = callback_data.sub_view or "overview"
    await callback.answer()

    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    target_currency = "USD"
    if user_db_id:
        user_row = await asyncio.to_thread(db_bot.execute_row, "SELECT base_currency FROM public.users WHERE id = %s;", (user_db_id,))
        if user_row and user_row.get("base_currency"):
            target_currency = user_row["base_currency"]
    target_cur_row = await asyncio.to_thread(db_bot.execute_row, "SELECT sign FROM public.currencies WHERE id = %s;", (target_currency,))
    target_sign = target_cur_row.get('sign', '$') if target_cur_row else '$'
    fx_rate = await asyncio.to_thread(convert_currency_amount, db_bot, 1.0, "USD", target_currency)

    data = await asyncio.to_thread(assemble_portfolio_digest_data, db_sys, p_id)
    capital_hint = None

    if s_id:
        strat_row = await asyncio.to_thread(
            db_sys.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (s_id,)
        )
        strategy_name = (strat_row or {}).get("strategy_name") or data["title"]
        data = filter_digest_data_by_strategy(data, s_id, strategy_name)
        back_text, back_action = "🔙 К стратегии", MenuAction(action="view_strategy", portfolio_id=p_id, strategy_id=s_id, sub_view="assets")

        header = await format_strategy_header(s_id)
        if "не найдена" in header:
            back_to = generate_nav_back_keyboard(one_step_back_text=back_text, full_back_callback=back_action.pack())
            try:
                await callback.message.edit_text(header, parse_mode="Markdown", reply_markup=back_to)
            except TelegramBadRequest:
                pass
            return
        header += await format_strategy_capital_block(p_id, s_id, target_sign=target_sign, fx_rate=fx_rate)
    else:
        back_text, back_action = "🔙 К портфелю", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets")
        header = await format_portfolio_header(p_id, target_currency=target_currency, target_sign=target_sign)
        capital_hint = f"🧮 Свободный бюджет по модели стратегий: **{target_sign}{data['real_cash'] * fx_rate:,.2f}**"

    if section_key == "overview" or section_key not in data["sections"]:
        text = header + render_digest_overview_text(data, standalone=False, capital_hint=capital_hint)
        toc_keyboard = generate_digest_toc_keyboard(p_id, data["sections"], strategy_id=s_id)
        final_builder = InlineKeyboardBuilder.from_markup(toc_keyboard) if toc_keyboard else InlineKeyboardBuilder()
        nav_kb = generate_nav_back_keyboard(one_step_back_text=back_text, full_back_callback=back_action.pack())
        final_builder.attach(InlineKeyboardBuilder.from_markup(nav_kb))
        keyboard = final_builder.as_markup()
    else:
        text = header + render_digest_section_text(data, section_key, standalone=False)
        keyboard = generate_digest_section_keyboard(
            p_id, section_key, data["sections"][section_key]["items"],
            execution_mode=data.get("execution_mode", "ADVISORY"), filter_strategy_id=s_id
        )

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "digest_stub"))
async def process_digest_stub(callback: types.CallbackQuery, callback_data: MenuAction):
    """Заглушка для разделов дайджеста, у которых целевой экран ещё не решён (см. BACKLOG.md п.35)."""
    await callback.answer("🔧 Этот раздел ещё в разработке.", show_alert=True)
