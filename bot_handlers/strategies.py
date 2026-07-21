import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_screens import format_strategy_header
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_portfolio_button_text
from analytics.portfolio_inspector import PortfolioInspector

router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_strategy"))
async def process_view_strategy(callback: types.CallbackQuery, callback_data: MenuAction):
    """Карточка стратегии: план/факт капитала, риск-аудит лимитов, список бумаг внутри стратегии."""
    s_id = callback_data.strategy_id
    p_id = callback_data.portfolio_id

    await callback.answer("Собираю карточку стратегии...")

    header = await format_strategy_header(s_id)
    back_to_portfolio = generate_nav_back_keyboard(
        one_step_back_text="🔙 К стратегиям портфеля",
        full_back_callback=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="strategies").pack()
    )

    if "не найдена" in header:
        try:
            await callback.message.edit_text(header, parse_mode="Markdown", reply_markup=back_to_portfolio)
        except TelegramBadRequest:
            pass
        return

    text = header

    # 0. Смысловой ключ "заводского" шаблона -- нужен, чтобы точно называть служебную
    # стратегию в сообщении "бумаг нет" (а не хардкодить конкретное имя, см. фидбек 2026-07-21)
    key_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT st.system_key FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.id = {int(s_id)};
        """
    )
    system_key = key_row.get("system_key") if key_row else None

    # 1. План/факт капитала -- переиспользуем готовый аналитический модуль (см. analytics/portfolio_inspector.py)
    inspector = await asyncio.to_thread(PortfolioInspector, db_bot, p_id)
    balances = await asyncio.to_thread(inspector.get_virtual_cash_balances)
    strat_balance = balances.get("strategies", {}).get(s_id)

    if strat_balance:
        ideal = float(strat_balance["ideal_budget_usd"])
        current = float(strat_balance["current_holdings_usd"])
        free = float(strat_balance["virtual_free_cash_usd"])
        text += (
            f"💰 **План/факт капитала:**\n"
            f" • Идеальный бюджет: **${ideal:,.2f}**\n"
            f" • Текущая стоимость позиций: **${current:,.2f}**\n"
            f" • Свободный остаток: **${free:,.2f}**\n"
            f"───────\n"
        )

    # 2. Риск-аудит лимитов из rules_config (по аналогии с "Паспортом качества" портфеля)
    audit = await asyncio.to_thread(inspector.audit_limits_and_rules)
    strat_audit = audit.get("strategies", {}).get(s_id)

    if strat_audit and strat_audit.get("violation_found"):
        text += "🛡️ **Нарушения лимитов стратегии:**\n"
        for a in strat_audit.get("violated_assets", []):
            text += f" ⚠️ {a['symbol']}: доля {a['current_share_pct']:.1f}% (лимит {a['limit_pct']}%)\n"
        for sec in strat_audit.get("violated_sectors", []):
            text += f" ⚠️ Сектор {sec.get('sector', '?')}: доля {sec.get('current_share_pct', 0):.1f}% (лимит {sec.get('limit_pct', 0)}%)\n"
        for t in strat_audit.get("tax_shield_breaches", []):
            text += f" ⚠️ {t['symbol']}: дивиденды {t['dividend_yield_pct']:.1f}% (лимит {t['limit_pct']}%)\n"
        text += "───────\n"
    else:
        text += "🛡️ Лимиты и налоговые риски стратегии соблюдены.\n───────\n"

    # 3. Список позиций внутри стратегии -- через универсальный слоистый view (см. Claude/02_universal_views.md)
    sql_positions = f"""
        SELECT listing_id, allocated_quantity, symbol, avg_price, listing_last_price
        FROM public.v_strategy_assets_full
        WHERE strategy_id = {int(s_id)} AND allocated_quantity > 0
        ORDER BY symbol ASC;
    """
    positions = await asyncio.to_thread(db_bot.execute_query, sql_positions)
    positions = positions if isinstance(positions, list) else []

    text += "📦 **Бумаги в этой стратегии:**"

    builder = InlineKeyboardBuilder()
    if positions:
        for pos in positions:
            qty = float(pos["allocated_quantity"])
            avg_p = float(pos["avg_price"] or 0)
            last_p = float(pos["listing_last_price"] or 0)

            position_cost = qty * avg_p
            position_market = qty * last_p
            position_profit = position_market - position_cost
            position_profit_pct = (position_profit / position_cost * 100) if position_cost > 0 else 0.0

            if position_profit > 0.01:
                crystal = "🟢"
            elif position_profit < -0.01:
                crystal = "🔴"
            else:
                crystal = "🔹"

            button_text = generate_portfolio_button_text(
                crystal=crystal,
                ticker=pos["symbol"],
                quantity=int(qty),
                profit=position_profit,
                profit_pct=position_profit_pct
            )

            builder.row(types.InlineKeyboardButton(
                text=button_text,
                callback_data=MenuAction(
                    action="view_ticker",
                    portfolio_id=p_id,
                    listing_id=int(pos["listing_id"]),
                    ticker_name=pos["symbol"],
                    sub_view="owner",
                    strategy_id=int(s_id)
                ).pack()
            ))
    else:
        if system_key == "CASH_RESERVE":
            text += "\n   *Бумаг в этой стратегии нет — это нормально для Кэш/Резерва.*"
        else:
            text += "\n   *Бумаг в этой стратегии пока нет.*"

    text += "\n───────\n"

    nav_markup = generate_nav_back_keyboard(
        one_step_back_text="🔙 К стратегиям портфеля",
        full_back_callback=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="strategies").pack()
    )
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(nav_markup))

    logging.info(f"🎯 [СТРАТЕГИЯ]: Отправляю карточку стратегии #{s_id} в Telegram...")
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass
