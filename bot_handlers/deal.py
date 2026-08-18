import asyncio
import logging

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard
from analytics.deal_planner import create_buy_plan, create_sell_plan, create_trim_plan

"""
«К сделке» -- единая точка входа для решения «беру»/«продаю» (Claude/BACKLOG.md
№122/123), заменяет digest_execute_buy/digest_execute_sell и не знает про
execution_mode вообще -- один и тот же путь для реального и бумажного портфеля,
разница только в том, что происходит ПОСЛЕ создания Плана (см. analytics/
deal_planner.py, analytics/ladder_step_watcher.py, brokers_connectors/paper_broker.py).
"""

router = Router()


def _cheat_sheet_keyboard(action: str, portfolio_id: int, strategy_id: int, ticker_id: int = 0, listing_id: int = 0):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="⚡ Как можно скорее",
        callback_data=MenuAction(
            action=action, portfolio_id=portfolio_id, strategy_id=strategy_id,
            ticker_id=ticker_id, listing_id=listing_id, sub_view="asap"
        ).pack()
    ))
    builder.row(types.InlineKeyboardButton(
        text="🎯 Оптимальная цена",
        callback_data=MenuAction(
            action=action, portfolio_id=portfolio_id, strategy_id=strategy_id,
            ticker_id=ticker_id, listing_id=listing_id, sub_view="optimal"
        ).pack()
    ))
    return builder


def _back_to_digest_keyboard(portfolio_id: int):
    return generate_nav_back_keyboard(
        one_step_back_text="🔙 Назад к дайджесту",
        full_back_callback=MenuAction(action="view_digest", portfolio_id=portfolio_id, sub_view="signals").pack()
    )


@router.callback_query(MenuAction.filter(F.action == "deal_start_buy"))
async def process_deal_start_buy(callback: types.CallbackQuery, callback_data: MenuAction):
    """Шаг 1 -- предлагает выбрать шпаргалку, ничего ещё не создаёт."""
    await callback.answer()
    keyboard = _cheat_sheet_keyboard(
        "deal_confirm_buy", callback_data.portfolio_id, callback_data.strategy_id, ticker_id=callback_data.ticker_id
    )
    keyboard.attach(InlineKeyboardBuilder.from_markup(_back_to_digest_keyboard(callback_data.portfolio_id)))
    try:
        await callback.message.edit_text(
            "🤝 Как действуем?\n\n"
            "⚡ *Как можно скорее* — заявка близко к текущей цене, минимум ожидания.\n"
            "🎯 *Оптимальная цена* — система подождёт более выгодную цену и сообщит, когда пора.",
            parse_mode="Markdown", reply_markup=keyboard.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "deal_confirm_buy"))
async def process_deal_confirm_buy(callback: types.CallbackQuery, callback_data: MenuAction):
    p_id, s_id, t_id = callback_data.portfolio_id, callback_data.strategy_id, callback_data.ticker_id
    cheat_sheet = callback_data.sub_view

    await callback.answer("Создаю план...")
    result = await asyncio.to_thread(create_buy_plan, db_sys, p_id, s_id, t_id, cheat_sheet)

    back_kb = _back_to_digest_keyboard(p_id)
    if not result["ok"]:
        try:
            await callback.message.edit_text(f"⚠️ {result['error']}", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    text = _format_plan_created_text(result)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "deal_start_sell"))
async def process_deal_start_sell(callback: types.CallbackQuery, callback_data: MenuAction):
    await callback.answer()
    keyboard = _cheat_sheet_keyboard(
        "deal_confirm_sell", callback_data.portfolio_id, callback_data.strategy_id, listing_id=callback_data.listing_id
    )
    keyboard.attach(InlineKeyboardBuilder.from_markup(_back_to_digest_keyboard(callback_data.portfolio_id)))
    try:
        await callback.message.edit_text(
            "🤝 Как действуем?\n\n"
            "⚡ *Как можно скорее* — заявка близко к текущей цене, минимум ожидания.\n"
            "🎯 *Оптимальная цена* — система подождёт более выгодную цену и сообщит, когда пора.",
            parse_mode="Markdown", reply_markup=keyboard.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "deal_confirm_sell"))
async def process_deal_confirm_sell(callback: types.CallbackQuery, callback_data: MenuAction):
    p_id, s_id, l_id = callback_data.portfolio_id, callback_data.strategy_id, callback_data.listing_id
    cheat_sheet = callback_data.sub_view

    await callback.answer("Создаю план...")
    result = await asyncio.to_thread(create_sell_plan, db_sys, p_id, s_id, l_id, cheat_sheet)

    back_kb = _back_to_digest_keyboard(p_id)
    if not result["ok"]:
        try:
            await callback.message.edit_text(f"⚠️ {result['error']}", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    text = _format_plan_created_text(result, is_sell=True)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "deal_trim"))
async def process_deal_trim(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Подрезка перевешенной позиции -- в отличие от покупки/продажи, без шпаргалки
    ASAP/оптимальная цена (согласовано с пользователем 2026-08-18: подрезка --
    гигиена портфеля, не решение о моменте, всегда рынком) -- один клик сразу
    создаёт План, минуя промежуточный экран выбора.
    """
    p_id, s_id, l_id = callback_data.portfolio_id, callback_data.strategy_id, callback_data.listing_id

    await callback.answer("Создаю план...")
    result = await asyncio.to_thread(create_trim_plan, db_sys, p_id, s_id, l_id)

    back_kb = _back_to_digest_keyboard(p_id)
    if not result["ok"]:
        try:
            await callback.message.edit_text(f"⚠️ {result['error']}", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    text = _format_plan_created_text(result, is_trim=True)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except TelegramBadRequest:
        pass


def _format_plan_created_text(result: dict, is_sell: bool = False, is_trim: bool = False) -> str:
    """
    Единый текст для реального И бумажного портфеля -- намеренно не говорит
    "исполнено", План только создан, дальше решает LadderStepWatcher/paper_broker
    (Claude/BACKLOG.md №117/122/123).
    """
    symbol = result["symbol"]
    qty = abs(result["qty"])
    override = result["override"]
    verb = "подрезки" if is_trim else ("продажи" if is_sell else "входа")

    if override["mode"] == "market":
        # Нейтрально по execution_mode (Claude/BACKLOG.md №131, 2026-08-18) -- раньше
        # "заявка уйдёт, как только рынок откроется" буквально верно только для бумажного
        # портфеля (paper_broker.py правда исполняет сам); на реальном заявку всегда
        # отправляет человек по сигналу "🪜 Пора шаг N" -- фраза вводила в заблуждение,
        # что систему сделает это сама. Убрана претензия на то, КТО действует.
        wait_text = "открытия рынка"
    else:
        wait_text = f"цены ${override['price']:,.2f}"

    return (
        f"✅ **План {verb} создан**\n\n"
        f"*{symbol}*, {qty:g} шт.\n"
        f"Жду {wait_text}.\n\n"
        f"Сообщу, когда условие выполнится."
    )
