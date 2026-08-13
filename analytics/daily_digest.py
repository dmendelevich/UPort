import datetime

import settings
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
    (п.5 БЭКЛОГА, 2026-07-30). Пометка рекомендаций «Купить»/«Продать»/TRIM_DOWN/TOP_UP при
    уже существующем активном приказе того же направления -- см. order_note() ниже (2026-08-03).
    """
    portfolio_row = db_instance.execute_row(
        "SELECT name FROM public.portfolios WHERE id = %s;", (portfolio_id,)
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
    ACTION_BADGES = {"SELL": "📤", "HOLD": "📧", "BUY": "📥", "LADDER": "🪜", "STALE": "🕰", "TRIM_DOWN": "✂️", "TOP_UP": "📥", "REVIEW": "📅"}

    # Пометка "уже есть активный приказ" (закрывает вторую половину BACKLOG.md п.5,
    # 2026-08-03) -- по факту существования приказа в нужную сторону на этом тикере, не
    # точное совпадение количества/цены с конкретным планом (это отдельная, более глубокая
    # задача). Помечаем, не скрываем -- "система только советует", у уже стоящего приказа
    # может быть другое количество/причина, прятать рекомендацию рискованно.
    active_orders_rows = db_instance.execute_query("""
        SELECT o.oper, l.ticker_id
        FROM public.orders o
        JOIN public.listings l ON o.listing_id = l.id
        WHERE o.portfolio_id = %s AND o.status = 'active';
    """, (portfolio_id,))
    active_orders_rows = active_orders_rows if isinstance(active_orders_rows, list) else ([active_orders_rows] if active_orders_rows else [])
    active_buy_ticker_ids = {int(r["ticker_id"]) for r in active_orders_rows if int(r["oper"]) in (1, 2)}
    active_sell_ticker_ids = {int(r["ticker_id"]) for r in active_orders_rows if int(r["oper"]) == 3}

    def order_note(ticker_id, direction_set):
        return " ⚠️ уже есть активный приказ" if ticker_id in direction_set else ""

    exit_alerts = PositionExitEvaluator(db_instance).evaluate_portfolio_exits(portfolio_id)
    schedule_items = [
        {
            "text": f"{ACTION_BADGES[a['recommendation']]} {a['symbol']} ({a['strategy_name']}): {a['reason']}"
                    + (order_note(a["ticker_id"], active_sell_ticker_ids) if a["recommendation"] == "SELL" else ""),
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in exit_alerts if a["trigger_kind"] == "calendar"
    ]
    signal_items = [
        {
            "text": f"{ACTION_BADGES[a['recommendation']]} {a['symbol']} ({a['strategy_name']}): {a['reason']}"
                    + (order_note(a["ticker_id"], active_sell_ticker_ids) if a["recommendation"] == "SELL" else ""),
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
    for v in audit_report.get("portfolio_violated_sectors", []):
        limit_items.append({
            "text": f"сектор {v['sector']}: {v['projected_pct']}% от ВСЕГО портфеля (лимит {v['limit_pct']}%)",
            "label": v["sector"],
        })

    cash_recs = CashDeploymentAdvisor(db_instance).evaluate_deployment(portfolio_id)
    signal_items += [
        {
            "text": f"{ACTION_BADGES['BUY']} {r['strategy_name']}: {r['reason']}"
                    + order_note(r["ticker_id"], active_buy_ticker_ids),
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
            "text": f"{ACTION_BADGES[a['recommendation']]} {a['symbol']} ({a['strategy_name']}): {a['reason']}"
                    + order_note(a["ticker_id"], active_sell_ticker_ids if a["recommendation"] == "TRIM_DOWN" else active_buy_ticker_ids),
            "label": a["symbol"],
            "listing_id": a["listing_id"],
        }
        for a in rebalance_alerts
    ]

    # Готовность следующего шага лесенки -- рыночно-зависимая проверка, решается на цикле
    # котировок (analytics/ladder_step_watcher.py::check_ladder_step_triggers), не в дайджесте
    # (обсуждено 2026-07-24). Дайджест только читает уже проставленный флаг, свежие цифры для
    # отображения считает на лету (listings.last_price), сам условие не пересчитывает.
    ladder_rows = db_instance.execute_query("""
        SELECT op.listing_id, t.symbol, s.strategy_name, op.current_step, op.target_quantity,
               st.budget_share_pct, l.last_price
        FROM public.order_pipelines op
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.strategy_tactics st ON st.strategy_id = op.strategy_id AND st.step_number = op.current_step
        WHERE op.portfolio_id = %s AND op.step_ready_notified_at IS NOT NULL
          AND op.pending_broker_order_id IS NULL;
    """, (portfolio_id,))
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

    # Ежемесячный пересмотр фиксированных слотов Р/Т (settings.MONTHLY_SLOT_REVIEW_DAY,
    # Claude/13_portfolio_construction_and_rebalancing_rules.md) -- только факты, ничего
    # не меняет сам, решение (поднять сумму или копить число слотов) -- за пользователем.
    if datetime.date.today().day == settings.MONTHLY_SLOT_REVIEW_DAY:
        review_rows = db_instance.execute_query("""
            SELECT v.strategy_id, v.strategy_name, v.rules_config,
                   (SELECT COUNT(*) FROM public.strategy_assets sa
                     WHERE sa.strategy_id = v.strategy_id AND sa.allocated_quantity > 0) AS position_count
            FROM public.v_strategies_full v
            WHERE v.portfolio_id = %s AND v.is_active = true
              AND v.system_key IN ('REVOLVER', 'TREND_FOLLOWING')
            ORDER BY v.display_order;
        """, (portfolio_id,))
        review_rows = review_rows if isinstance(review_rows, list) else ([review_rows] if review_rows else [])
        for row in review_rows:
            slot_fixed = (row.get("rules_config") or {}).get("tactic_slot_fixed_usd")
            if slot_fixed is None:
                continue
            bal = balances.get("strategies", {}).get(int(row["strategy_id"]))
            if not bal:
                continue
            ideal_budget = float(bal.get("ideal_budget_usd") or 0.0)
            pct_of_target = (float(bal.get("current_holdings_usd") or 0.0) / ideal_budget * 100.0) if ideal_budget > 0 else 0.0
            schedule_items.append({
                "text": (
                    f"{ACTION_BADGES['REVIEW']} {row['strategy_name']}: слот ${float(slot_fixed):,.0f}, "
                    f"{int(row['position_count'])} поз. держится, стратегия на {pct_of_target:.1f}% от цели. "
                    f"Ежемесячный пересмотр — поднять слот или продолжать копить число позиций?"
                ),
                "label": row["strategy_name"],
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
