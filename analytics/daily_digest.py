import datetime

from analytics.position_exit_evaluator import PositionExitEvaluator
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.portfolio_inspector import PortfolioInspector


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

    today_str = datetime.date.today().isoformat()
    lines = [f"📊 *{portfolio_name}* — дайджест на {today_str}", ""]
    lines.append(f"💰 Капитал: ${total_capital:,.2f} · Кэш: ${real_cash:,.2f}")
    lines.append(
        f"🔴 к продаже: {len(sell_alerts)} · ⚠️ нарушений лимитов: {len(limit_warnings)} · "
        f"🟢 идей для покупки: {len(buy_recs)}"
    )
    lines.append("")

    has_actions = bool(sell_alerts or hold_alerts or limit_warnings or buy_recs)
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

    return "\n".join(lines)
