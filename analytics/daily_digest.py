import datetime

from analytics.position_exit_evaluator import PositionExitEvaluator
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import expected_step_quantity

# Порядок и подписи разделов дайджеста -- единый источник правды для сборки данных
# и для рендера (bot_screens.py) / клавиатур (bot_keyboards.py). См. Claude/BACKLOG.md
# п.35 (оглавление дайджеста) и Claude/11_asset_lifecycle_and_plan.md.
SECTION_ORDER = ("sell", "hold", "limits", "buy", "ladder", "stale")
SECTION_META = {
    "sell": {"emoji": "🔴", "label": "Продать"},
    "hold": {"emoji": "🟡", "label": "Придержать"},
    "limits": {"emoji": "⚠️", "label": "Лимиты"},
    "buy": {"emoji": "🟢", "label": "Купить"},
    "ladder": {"emoji": "🪜", "label": "Лесенка"},
    "stale": {"emoji": "🕰", "label": "Устаревшие"},
}


def assemble_portfolio_digest_data(db_instance, portfolio_id: int) -> dict:
    """
    Собирает утренний дайджест по одному портфелю как СТРУКТУРУ ДАННЫХ (не текст) --
    пульс капитала + шесть разделов действий на сегодня. Чистая сборка -- не знает про
    Telegram/aiogram, только читает уже готовую аналитику (PositionExitEvaluator,
    CashDeploymentAdvisor, PortfolioInspector). Рендер текста -- bot_screens.py
    (render_digest_overview_text/render_digest_section_text), клавиатуры -- bot_keyboards.py.

    Каждый пункт раздела -- словарь с обязательным "text" (человекочитаемая строка) и
    "label" (короткая подпись для кнопки), плюс один из навигационных ключей:
    "listing_id" (открыть карточку тикера -- бумага уже держится/есть приказ) или
    "ticker_id" (кандидат на покупку, листинга в этом портфеле может ещё не быть).
    У пунктов раздела "limits" навигационных ключей нет -- целевой экран пока не решён
    (см. Claude/BACKLOG.md), кнопка-заглушка.

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
    sell_items = [
        {
            "text": f"{a['symbol']} ({a['strategy_name']}): {a['reason']}",
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in exit_alerts if a["recommendation"] == "SELL"
    ]
    hold_items = [
        {
            "text": f"{a['symbol']} ({a['strategy_name']}): {a['reason']}",
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in exit_alerts if a["recommendation"] == "HOLD"
    ]

    audit_report = inspector.audit_limits_and_rules()
    limit_items = []
    for strat_report in audit_report.get("strategies", {}).values():
        for v in strat_report.get("violated_assets", []):
            limit_items.append({
                "text": f"{v['symbol']}: {v['current_share_pct']}% от портфеля (лимит {v['limit_pct']}%)",
                "label": v["symbol"],
            })
        for v in strat_report.get("violated_sectors", []):
            limit_items.append({
                "text": f"сектор {v['sector']}: {v['current_share_pct']}% от портфеля (лимит {v['limit_pct']}%)",
                "label": v["sector"],
            })
        for v in strat_report.get("tax_shield_breaches", []):
            limit_items.append({
                "text": f"{v['symbol']}: дивиденды {v['dividend_yield_pct']}% (лимит {v['limit_pct']}%)",
                "label": v["symbol"],
            })

    cash_recs = CashDeploymentAdvisor(db_instance).evaluate_deployment(portfolio_id)
    buy_items = [
        {
            "text": f"{r['strategy_name']}: {r['reason']}",
            "label": r["symbol"],
            "ticker_id": r["ticker_id"],
        }
        for r in cash_recs if r["status"] == "CANDIDATE_FOUND"
    ]

    # Готовность следующего шага лесенки -- рыночно-зависимая проверка, решается на цикле
    # котировок (analytics/ladder_step_watcher.py::check_ladder_step_triggers), не в дайджесте
    # (обсуждено 2026-07-24). Дайджест только читает уже проставленный флаг, свежие цифры для
    # отображения считает на лету (listings.last_price), сам условие не пересчитывает.
    ladder_rows = db_instance.execute_query(f"""
        SELECT op.listing_id, t.symbol, s.strategy_name, op.current_step, op.target_quantity,
               st.budget_share_pct, l.last_price
        FROM public.order_pipelines op
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.strategy_tactics st ON st.strategy_id = op.strategy_id AND st.step_number = op.current_step
        WHERE op.portfolio_id = {int(portfolio_id)} AND op.step_ready_notified_at IS NOT NULL;
    """)
    ladder_rows = ladder_rows if isinstance(ladder_rows, list) else ([ladder_rows] if ladder_rows else [])
    ladder_items = []
    for step in ladder_rows:
        qty = expected_step_quantity(step["target_quantity"], step["budget_share_pct"])
        price = float(step["last_price"] or 0)
        ladder_items.append({
            "text": (
                f"{step['symbol']} ({step['strategy_name']}), шаг {step['current_step']}: "
                f"условие выполнено, ~{abs(qty):.0f} шт по ${price:,.2f}"
            ),
            "label": step["symbol"],
            "listing_id": step["listing_id"],
        })

    stale_rows = db_instance.execute_query(f"""
        SELECT op.listing_id, t.symbol, s.strategy_name, o.p AS order_price, l.last_price
        FROM public.order_pipelines op
        JOIN public.orders o ON o.broker_order_id = op.pending_broker_order_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        WHERE op.portfolio_id = {int(portfolio_id)} AND op.stale_notified_at IS NOT NULL;
    """)
    stale_rows = stale_rows if isinstance(stale_rows, list) else ([stale_rows] if stale_rows else [])
    stale_items = []
    for so in stale_rows:
        order_price = float(so["order_price"] or 0)
        last_price = float(so["last_price"] or 0)
        drift_pct = abs(last_price - order_price) / order_price * 100.0 if order_price else 0.0
        stale_items.append({
            "text": (
                f"{so['symbol']} ({so['strategy_name']}): приказ по ${order_price:,.2f}, "
                f"текущая цена ${last_price:,.2f} ({drift_pct:.1f}%). Пересмотрите или отмените."
            ),
            "label": so["symbol"],
            "listing_id": so["listing_id"],
        })

    return {
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio_name,
        "today_str": datetime.date.today().isoformat(),
        "total_capital": total_capital,
        "real_cash": real_cash,
        "sections": {
            "sell": {**SECTION_META["sell"], "items": sell_items},
            "hold": {**SECTION_META["hold"], "items": hold_items},
            "limits": {**SECTION_META["limits"], "items": limit_items},
            "buy": {**SECTION_META["buy"], "items": buy_items},
            "ladder": {**SECTION_META["ladder"], "items": ladder_items},
            "stale": {**SECTION_META["stale"], "items": stale_items},
        },
    }
