import datetime

from analytics.position_exit_evaluator import PositionExitEvaluator
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import expected_step_quantity


def assemble_portfolio_digest(db_instance, portfolio_id: int) -> str:
    """
    Собирает утренний дайджест по одному портфелю: пульс капитала + список
    действий на сегодня (продать/придержать, нарушения лимитов, идея для покупки).
    Чистая текстовая сборка -- не знает про Telegram/aiogram, только читает уже
    готовую аналитику (PositionExitEvaluator, CashDeploymentAdvisor, PortfolioInspector).

    Сознательно НЕ входит в v0 (см. BACKLOG.md): проверка/редактирование уже
    выставленных приказов и алертов -- обязательный пункт V1.
    """
    portfolio_row = db_instance.execute_row(
        f"SELECT name FROM public.portfolios WHERE id = {int(portfolio_id)};"
    )
    portfolio_name = (portfolio_row or {}).get("name") or f"Портфель {portfolio_id}"

    inspector = PortfolioInspector(db_instance, portfolio_id)
    balances = inspector.get_virtual_cash_balances()
    total_capital = float(balances.get("total_capital_usd") or 0.0)
    # Реальный кэш = сумма virtual_free_cash_usd по всем стратегиям (тождество,
    # см. analytics/cash_deployment_advisor.py) -- отдельный запрос к accounts не нужен.
    real_cash = sum(
        float(v.get("virtual_free_cash_usd") or 0.0)
        for v in balances.get("strategies", {}).values()
    )

    exit_alerts = PositionExitEvaluator(db_instance).evaluate_portfolio_exits(portfolio_id)
    sell_alerts = [a for a in exit_alerts if a["recommendation"] == "SELL"]
    hold_alerts = [a for a in exit_alerts if a["recommendation"] == "HOLD"]

    audit_report = inspector.audit_limits_and_rules()
    limit_warnings = []
    for strat_report in audit_report.get("strategies", {}).values():
        for v in strat_report.get("violated_assets", []):
            limit_warnings.append(
                f"{v['symbol']}: {v['current_share_pct']}% от портфеля (лимит {v['limit_pct']}%)"
            )
        for v in strat_report.get("violated_sectors", []):
            limit_warnings.append(
                f"сектор {v['sector']}: {v['current_share_pct']}% от портфеля (лимит {v['limit_pct']}%)"
            )
        for v in strat_report.get("tax_shield_breaches", []):
            limit_warnings.append(
                f"{v['symbol']}: дивиденды {v['dividend_yield_pct']}% (лимит {v['limit_pct']}%)"
            )

    cash_recs = CashDeploymentAdvisor(db_instance).evaluate_deployment(portfolio_id)
    buy_recs = [r for r in cash_recs if r["status"] == "CANDIDATE_FOUND"]

    # Готовность следующего шага лесенки -- рыночно-зависимая проверка, решается на цикле
    # котировок (analytics/ladder_step_watcher.py::check_ladder_step_triggers), не в дайджесте
    # (обсуждено 2026-07-24). Дайджест только читает уже проставленный флаг, свежие цифры для
    # отображения считает на лету (listings.last_price), сам условие не пересчитывает.
    ladder_steps = db_instance.execute_query(f"""
        SELECT t.symbol, s.strategy_name, op.current_step, op.target_quantity,
               st.budget_share_pct, l.last_price
        FROM public.order_pipelines op
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.strategy_tactics st ON st.strategy_id = op.strategy_id AND st.step_number = op.current_step
        WHERE op.portfolio_id = {int(portfolio_id)} AND op.step_ready_notified_at IS NOT NULL;
    """)
    ladder_steps = ladder_steps if isinstance(ladder_steps, list) else ([ladder_steps] if ladder_steps else [])

    stale_orders = db_instance.execute_query(f"""
        SELECT t.symbol, s.strategy_name, o.p AS order_price, l.last_price
        FROM public.order_pipelines op
        JOIN public.orders o ON o.broker_order_id = op.pending_broker_order_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        WHERE op.portfolio_id = {int(portfolio_id)} AND op.stale_notified_at IS NOT NULL;
    """)
    stale_orders = stale_orders if isinstance(stale_orders, list) else ([stale_orders] if stale_orders else [])

    today_str = datetime.date.today().isoformat()
    lines = [f"📊 *{portfolio_name}* — дайджест на {today_str}", ""]
    lines.append(f"💰 Капитал: ${total_capital:,.2f} · Кэш: ${real_cash:,.2f}")
    lines.append(
        f"🔴 к продаже: {len(sell_alerts)} · ⚠️ нарушений лимитов: {len(limit_warnings)} · "
        f"🟢 идей для покупки: {len(buy_recs)} · 🪜 шагов лесенки: {len(ladder_steps)} · "
        f"🕰 устаревших приказов: {len(stale_orders)}"
    )
    lines.append("")

    has_actions = bool(sell_alerts or hold_alerts or limit_warnings or buy_recs or ladder_steps or stale_orders)
    if not has_actions:
        lines.append("Действий сегодня не требуется — всё по плану.")
        return "\n".join(lines)

    lines.append("━━━ Действия на сегодня ━━━")

    if sell_alerts:
        lines.append("")
        lines.append("🔴 *ПРОДАТЬ:*")
        for a in sell_alerts:
            lines.append(f"• {a['symbol']} ({a['strategy_name']}): {a['reason']}")

    if hold_alerts:
        lines.append("")
        lines.append("🟡 *ПРИДЕРЖАТЬ:*")
        for a in hold_alerts:
            lines.append(f"• {a['symbol']} ({a['strategy_name']}): {a['reason']}")

    if limit_warnings:
        lines.append("")
        lines.append("⚠️ *ЛИМИТЫ:*")
        for w in limit_warnings:
            lines.append(f"• {w}")

    if buy_recs:
        lines.append("")
        lines.append("🟢 *КУПИТЬ:*")
        for r in buy_recs:
            lines.append(f"• {r['strategy_name']}: {r['reason']}")

    if ladder_steps:
        lines.append("")
        lines.append("🪜 *СЛЕДУЮЩИЙ ШАГ ЛЕСЕНКИ (уже отмечено на котировках):*")
        for step in ladder_steps:
            qty = expected_step_quantity(step["target_quantity"], step["budget_share_pct"])
            price = float(step["last_price"] or 0)
            lines.append(
                f"• {step['symbol']} ({step['strategy_name']}), шаг {step['current_step']}: "
                f"условие выполнено, ~{abs(qty):.0f} шт по ${price:,.2f}"
            )

    if stale_orders:
        lines.append("")
        lines.append("🕰 *УСТАРЕВШИЕ ПРИКАЗЫ:*")
        for so in stale_orders:
            order_price = float(so["order_price"] or 0)
            last_price = float(so["last_price"] or 0)
            drift_pct = abs(last_price - order_price) / order_price * 100.0 if order_price else 0.0
            lines.append(
                f"• {so['symbol']} ({so['strategy_name']}): приказ по ${order_price:,.2f}, "
                f"текущая цена ${last_price:,.2f} ({drift_pct:.1f}%). Пересмотрите или отмените."
            )

    return "\n".join(lines)
