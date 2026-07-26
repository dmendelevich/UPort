import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_screens import format_strategy_header, format_premium_header
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_portfolio_button_text
from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import TickerEvaluator

router = Router()

# Человекочитаемые подписи для metrics из explain_map -- только для отображения
# пользователю в "обосновании", сами условия отбора живут в analytics_utils.py.
METRIC_LABELS = {
    "idea_min_turnover_usd": "Мин. оборот в день ($)",
    "idea_rsi_oversold_num": "RSI (перепроданность)",
    "speculative_catalyst": "Катализатор (MACD / рекомендация)",
    "idea_require_positive_fflow_bool": "Свободный денежный поток",
    "idea_report_buffer_days": "Дней до/после отчёта",
    "exchange_mic": "Биржа",
    "return_on_equity": "ROE",
    "debt_to_equity": "Долг/Капитал",
    "revenue_cagr_3y": "Рост выручки (3 года)",
    "revenue_growth": "Рост выручки (текущий)",
    "pe_trailing": "P/E",
    "signal_price_to_sma200_pct": "Отклонение от SMA200",
    "signal_rsi": "RSI",
    "moving_averages_fan": "Веер скользящих средних",
    "signal_macd": "MACD",
    "tactic_volume_surge_pct": "Всплеск объёма торгов",
}
STATUS_ICONS = {"PASS": "✅", "FAIL": "❌", "N/A": "➖", "WARNING": "⚠️"}


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

    ideas_builder = InlineKeyboardBuilder()
    if system_key in ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING"):
        ideas_builder.row(types.InlineKeyboardButton(
            text="💡 Предложения",
            callback_data=MenuAction(action="strategy_ideas", portfolio_id=p_id, strategy_id=int(s_id)).pack()
        ))

    nav_markup = generate_nav_back_keyboard(
        one_step_back_text="🔙 К стратегиям портфеля",
        full_back_callback=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="strategies").pack()
    )
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(ideas_builder)
    final_builder.attach(InlineKeyboardBuilder.from_markup(nav_markup))

    logging.info(f"🎯 [СТРАТЕГИЯ]: Отправляю карточку стратегии #{s_id} в Telegram...")
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "strategy_ideas"))
async def process_strategy_ideas(callback: types.CallbackQuery, callback_data: MenuAction):
    """Список инвестидей (топ-10) для стратегии: быстрый скан через TickerEvaluator.screen_universe_for_strategy."""
    s_id = callback_data.strategy_id
    p_id = callback_data.portfolio_id

    await callback.answer("Ищу кандидатов...")

    key_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT st.system_key FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.id = {int(s_id)};
        """
    )
    system_key = (key_row or {}).get("system_key")

    back_to_strategy = generate_nav_back_keyboard(
        one_step_back_text="🔙 К стратегии",
        full_back_callback=MenuAction(action="view_strategy", portfolio_id=p_id, strategy_id=s_id).pack()
    )

    if system_key not in ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING"):
        try:
            await callback.message.edit_text(
                "💡 У этой стратегии нет правила входа (служебная стратегия).",
                reply_markup=back_to_strategy
            )
        except TelegramBadRequest:
            pass
        return

    broker_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT b.short_name FROM public.portfolios p
            JOIN public.brokers b ON p.broker_id = b.id
            WHERE p.id = {int(p_id)};
        """
    )
    us_only = (broker_row or {}).get("short_name") == "FB"

    held_rows = await asyncio.to_thread(
        db_bot.execute_query,
        f"""
            SELECT DISTINCT lt.id AS ticker_id
            FROM public.assets a
            JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
            WHERE a.portfolio_id = {int(p_id)} AND a.quantity > 0;
        """
    )
    held_rows = held_rows if isinstance(held_rows, list) else ([held_rows] if held_rows else [])
    held_ids = {int(r["ticker_id"]) for r in held_rows if r}

    evaluator = TickerEvaluator(db_instance=db_bot)
    results = await asyncio.to_thread(
        evaluator.screen_universe_for_strategy, s_id, held_ids, us_only
    )
    top10 = results[:10]

    builder = InlineKeyboardBuilder()
    if top10:
        text = f"💡 *Инвестидеи для стратегии* (топ-{len(top10)}):\n\nНажми на бумагу, чтобы увидеть обоснование."
        for cand in top10:
            builder.row(types.InlineKeyboardButton(
                text=f"{cand['symbol']} (ranking={cand['ranking_value']})",
                callback_data=MenuAction(
                    action="view_idea_reason",
                    portfolio_id=p_id,
                    strategy_id=s_id,
                    ticker_id=int(cand["ticker_id"])
                ).pack()
            ))
    else:
        text = "💡 Сейчас ни одна бумага не проходит экран этой стратегии."

    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_to_strategy))

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "view_idea_reason"))
async def process_view_idea_reason(callback: types.CallbackQuery, callback_data: MenuAction):
    """Обоснование конкретного кандидата: PASS/FAIL по каждому критерию из explain_map."""
    s_id = callback_data.strategy_id
    p_id = callback_data.portfolio_id
    t_id = callback_data.ticker_id

    await callback.answer()

    evaluator = TickerEvaluator(db_instance=db_bot)
    report = await asyncio.to_thread(evaluator.evaluate_ticker_strategy, t_id, p_id)

    back_to_ideas = generate_nav_back_keyboard(
        one_step_back_text="🔙 К списку идей",
        full_back_callback=MenuAction(action="strategy_ideas", portfolio_id=p_id, strategy_id=s_id).pack()
    )

    info = report.get("explain_map", {}).get(s_id)
    if not info:
        try:
            await callback.message.edit_text(
                "⚠️ Не удалось пересчитать обоснование (возможно, бумага больше не подходит под стратегию).",
                reply_markup=back_to_ideas
            )
        except TelegramBadRequest:
            pass
        return

    header_text = await format_premium_header(t_id, p_id)
    text = header_text
    text += f"🎯 Обоснование для «{info['strategy_name']}»:\n\n"
    for key, m in info["metrics"].items():
        icon = STATUS_ICONS.get(m["status"], "•")
        label = METRIC_LABELS.get(key, key)
        text += f"{icon} {label}: факт `{m['fact']}`, порог `{m['limit']}`\n"

    verdict = "✅ Проходит экран стратегии" if info["is_compatible_technically"] else "❌ Не проходит экран стратегии"
    text += f"\n*{verdict}*"

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_ideas)
    except TelegramBadRequest:
        pass
