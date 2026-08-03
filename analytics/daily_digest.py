import datetime

from analytics.position_exit_evaluator import PositionExitEvaluator
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.portfolio_inspector import PortfolioInspector
from analytics.portfolio_rebalancer import PortfolioRebalancer
from analytics.analytics_utils import expected_step_quantity
from analytics.order_alert_staleness import evaluate_portfolio_staleness

# Порядок и подписи разделов дайджеста -- единый источник правды для сборки данных
# и для рендера (bot_screens.py) / клавиатур (bot_keyboards.py). См. Claude/BACKLOG.md
# и Claude/11_asset_lifecycle_and_plan.md.
#
# Реорганизовано 2026-07-30: разделы группируют по ПРИЧИНЕ появления пункта, а не по
# глаголу действия -- прежние 6 разделов (Продать/Придержать/Купить/Лесенка/Устаревшие/
# Лимиты) смешивали "что делать" и "почему", из-за чего у ребалансировки не было
# естественного дома (не то продать, не то купить). Теперь:
#   "schedule" -- сработало от ПРОШЕДШЕГО ВРЕМЕНИ (календарный триггер), не от цены.
#                 Сегодня единственный живой пример -- тайм-аут слота Револьверной.
#   "signals"  -- сработало от цены/RSI/фундаментала/наличия кэша: экс-Продать/Придержать/
#                 Купить/Лесенка/Устаревшие -- всё это один и тот же тип причины ("рынок
#                 или капитал сигналят"), просто с разным итогом. Каждый пункт несёт свою
#                 пометку действия в тексте (📤 продать / 📧 придержать / 📥 купить и т.п.),
#                 раздел их не разделяет.
#   "limits"   -- нарушение ЛИМИТА портфеля (концентрация/сектор/налоговый щит) -- без
#                 изменений, отдельная причина по своей природе (не про стратегию бумаги).
SECTION_ORDER = ("schedule", "signals", "limits")
SECTION_META = {
    "schedule": {"emoji": "⏰", "label": "По расписанию"},
    "signals": {"emoji": "📊", "label": "Сигналы по бумагам и деньгам"},
    "limits": {"emoji": "⚠️", "label": "Лимиты"},
}


def assemble_portfolio_digest_data(db_instance, portfolio_id: int) -> dict:
    """
    Собирает утренний дайджест по одному портфелю как СТРУКТУРУ ДАННЫХ (не текст) --
    пульс капитала + три раздела действий на сегодня (см. SECTION_META выше). Чистая
    сборка -- не знает про
    Telegram/aiogram, только читает уже готовую аналитику (PositionExitEvaluator,
    CashDeploymentAdvisor, PortfolioInspector). Рендер текста -- bot_screens.py
    (render_digest_overview_text/render_digest_section_text), клавиатуры -- bot_keyboards.py.

    Каждый пункт раздела -- словарь с обязательным "text" (человекочитаемая строка) и
    "label" (короткая подпись для кнопки), плюс один из навигационных ключей:
    "listing_id" (открыть карточку тикера -- бумага уже держится/есть приказ) или
    "ticker_id" (кандидат на покупку, листинга в этом портфеле может ещё не быть).
    У пунктов раздела "limits" навигационных ключей нет -- целевой экран пока не решён
    (см. Claude/BACKLOG.md), кнопка-заглушка.

    Протухание уже выставленных приказов/алертов -- см. analytics/order_alert_staleness.py
    (п.5 БЭКЛОГА, 2026-07-30). Сознательно НЕ входит: подавление/пометка рекомендаций
    «Купить»/«Продать» при уже существующем активном приказе -- отдельная тема.
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

    # Пометки действия внутри укрупнённого раздела "signals" -- раздел группирует по
    # ПРИЧИНЕ (рынок/капитал сигналят), не по глаголу, поэтому каждый пункт несёт свою
    # пометку в тексте, чтобы не потерять, что именно предлагается сделать.
    ACTION_BADGES = {"SELL": "📤", "HOLD": "📧", "BUY": "📥", "LADDER": "🪜", "STALE": "🕰", "TRIM": "✂️"}

    exit_alerts = PositionExitEvaluator(db_instance).evaluate_portfolio_exits(portfolio_id)
    schedule_items = [
        {
            "text": f"{ACTION_BADGES[a['recommendation']]} {a['symbol']} ({a['strategy_name']}): {a['reason']}",
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in exit_alerts if a["trigger_kind"] == "calendar"
    ]
    signal_items = [
        {
            "text": f"{ACTION_BADGES[a['recommendation']]} {a['symbol']} ({a['strategy_name']}): {a['reason']}",
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in exit_alerts if a["trigger_kind"] != "calendar"
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
    signal_items += [
        {
            "text": f"{ACTION_BADGES['BUY']} {r['strategy_name']}: {r['reason']}",
            "label": r["symbol"],
            "ticker_id": r["ticker_id"],
        }
        for r in cash_recs if r["status"] == "CANDIDATE_FOUND"
    ]

    # Подрезка перевешенных позиций (Claude/13_portfolio_construction_and_rebalancing_rules.md,
    # 2026-08-03) -- сегодня только Консервативная, см. PortfolioRebalancer.
    rebalance_alerts = PortfolioRebalancer(db_instance).evaluate_portfolio(portfolio_id)
    signal_items += [
        {
            "text": f"{ACTION_BADGES['TRIM']} {a['symbol']} ({a['strategy_name']}): {a['reason']}",
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in rebalance_alerts
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
    for step in ladder_rows:
        qty = expected_step_quantity(step["target_quantity"], step["budget_share_pct"])
        price = float(step["last_price"] or 0)
        signal_items.append({
            "text": (
                f"{ACTION_BADGES['LADDER']} {step['symbol']} ({step['strategy_name']}), шаг {step['current_step']}: "
                f"условие выполнено, ~{abs(qty):.0f} шт по ${price:,.2f}"
            ),
            "label": step["symbol"],
            "listing_id": step["listing_id"],
        })

    # Протухание приказов/алертов -- единый оценщик (см. Claude/BACKLOG.md, п.5,
    # 2026-07-30), заменяет узкий analytics/stale_order_watcher.py (только Plan-приказы,
    # push в Telegram) на чистую функцию по ВСЕМ активным приказам/алертам портфеля.
    # Ось 1 (цена) -- в "signals" (сработало от рынка); ось 2 (время) -- в "schedule"
    # (сработало от календаря), та же логика раздела, что и у остальных источников выше.
    staleness = evaluate_portfolio_staleness(db_instance, portfolio_id)
    kind_label = {"order": "приказ", "alert": "алерт"}
    for item in staleness["price"]:
        signal_items.append({
            "text": (
                f"{ACTION_BADGES['STALE']} {item['symbol']} ({kind_label[item['kind']]} по ${item['order_price']:,.2f}, "
                f"сейчас ${item['current_price']:,.2f}, {item['gap_pct']:.1f}%): необычно для этой бумаги. "
                f"Пересмотрите или отмените."
            ),
            "label": item["symbol"],
            "listing_id": item["listing_id"],
        })
    for item in staleness["time"]:
        schedule_items.append({
            "text": (
                f"{ACTION_BADGES['STALE']} {item['symbol']} ({kind_label[item['kind']]} висит {item['age_days']} дн. "
                f"без изменений): актуален ли ещё тезис?"
            ),
            "label": item["symbol"],
            "listing_id": item["listing_id"],
        })

    return {
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio_name,
        "today_str": datetime.date.today().isoformat(),
        "total_capital": total_capital,
        "real_cash": real_cash,
        "sections": {
            "schedule": {**SECTION_META["schedule"], "items": schedule_items},
            "signals": {**SECTION_META["signals"], "items": signal_items},
            "limits": {**SECTION_META["limits"], "items": limit_items},
        },
    }
