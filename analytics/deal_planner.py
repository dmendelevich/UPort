import json
import logging

from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.execution_price_advisor import suggest_execution_terms
from analytics.execution_price_advisor import suggest_optimal_price
from analytics.portfolio_rebalancer import PortfolioRebalancer
from analytics.analytics_utils import resolve_trim_shares

"""
«К сделке» -- единая точка входа для решения «беру» (Claude/BACKLOG.md №122/123),
заменяет разом «📝 План входа», «В список наблюдения» и раздельные пути реального/
бумажного портфеля. Не знает про execution_mode вообще -- создаёт ровно тот же
order_pipelines(PENDING, шаг 1) для обоих типов портфеля, разница в том, что
происходит ПОСЛЕ (человек у брокера vs эмулятор, см. analytics/ladder_step_watcher.py
и brokers_connectors/paper_broker.py).
"""


def resolve_cheat_sheet(db_instance, listing_id: int, action: str, cheat_sheet: str) -> dict:
    """
    Переводит выбор человека (шпаргалка А/Б) в готовый entry_trigger_override.
    cheat_sheet: "asap" -- suggest_execution_terms (лимитка близко к текущей цене,
    если сейчас видна аномалия, иначе рынок); "optimal" -- suggest_optimal_price
    (K×волатильность от текущей цены, всегда конкретная цель, не рынок).
    Возвращает {"mode": "market"} или {"mode": "limit_fixed", "price": X, "direction": action},
    либо {"error": "..."}, если данных не хватает.
    """
    if cheat_sheet == "asap":
        terms = suggest_execution_terms(db_instance, listing_id, action)
        if terms["mode"] == "limit" and terms.get("price"):
            return {"mode": "limit_fixed", "price": terms["price"], "direction": action}
        return {"mode": "market"}
    elif cheat_sheet == "optimal":
        opt = suggest_optimal_price(db_instance, listing_id, action)
        if opt.get("price"):
            return {"mode": "limit_fixed", "price": opt["price"], "direction": action}
        return {"error": "Недостаточно данных (цена/волатильность) для оптимальной цены -- попробуй позже."}
    else:
        raise ValueError(f"cheat_sheet должен быть 'asap' или 'optimal', получено: {cheat_sheet!r}")


def create_buy_plan(db_instance, portfolio_id: int, strategy_id: int, ticker_id: int, cheat_sheet: str) -> dict:
    """
    Перепроверяет кандидата заново (не по цифрам утреннего дайджеста), легализует
    листинг при необходимости, считает размер слота -- та же формула, что и у
    прежнего digest_execute_buy/paper_buy_yes (verify_buy_candidate + compute_slot_size).
    Заводит План (PENDING, шаг 1) с выбранной шпаргалкой в entry_trigger_override --
    НЕ выдаёт инструкцию сразу, дальше решает LadderStepWatcher/paper_broker.

    Возвращает {"ok": True, "pipeline_id", "symbol", "qty", "override"} или
    {"ok": False, "error": "..."}.
    """
    advisor = CashDeploymentAdvisor(db_instance)
    match = advisor.verify_buy_candidate(portfolio_id, strategy_id, ticker_id)
    if not match:
        return {"ok": False, "error": "Условия изменились -- кандидат больше не проходит экран стратегии."}
    symbol = match["symbol"]

    portfolio_row = db_instance.execute_row("SELECT broker_id FROM public.portfolios WHERE id = %s;", (portfolio_id,))
    broker_id = int((portfolio_row or {}).get("broker_id") or 1)

    listing_row = db_instance.execute_row(
        "SELECT id, last_price FROM public.listings WHERE ticker_id = %s AND broker_id = %s;", (ticker_id, broker_id)
    )
    if not listing_row:
        try:
            new_listing_id = db_instance.ensure_listing(ticker_id, broker_id)
        except Exception as e:
            return {"ok": False, "error": f"Не удалось легализовать листинг: {e}"}
        listing_row = db_instance.execute_row("SELECT id, last_price FROM public.listings WHERE id = %s;", (new_listing_id,))

    listing_id = int(listing_row["id"])
    price = float(listing_row.get("last_price") or 0.0)
    if price <= 0:
        return {"ok": False, "error": "Не удалось получить цену, попробуй позже.", "listing_id": listing_id}

    slot_usd = advisor.compute_slot_size(portfolio_id, strategy_id)
    cash_row = db_instance.execute_row(
        "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';", (portfolio_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)
    slot_usd = min(slot_usd, cash_available)
    if slot_usd <= 0:
        return {"ok": False, "error": "Стратегия уже на цели, свободного места под новую позицию нет.", "listing_id": listing_id}
    qty = max(1, round(slot_usd / price))

    override = resolve_cheat_sheet(db_instance, listing_id, "BUY", cheat_sheet)
    if "error" in override:
        return {"ok": False, "error": override["error"], "listing_id": listing_id}

    # execute_query НЕ бросает исключение на ошибку СУБД (уникальный индекс
    # order_pipelines_unique_active_run -- "уже есть активный план") -- она логирует
    # у шлюза и возвращает [] (см. database.py::execute_query), поэтому проверяем
    # пустой результат, не try/except (тот же паттерн, что и в остальном коде проекта).
    result = db_instance.execute_query(
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, entry_trigger_override, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 1, 'PENDING', %s, %s, NULL, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (portfolio_id, listing_id, ticker_id, strategy_id, qty, price, json.dumps(override))
    )
    if not result:
        return {
            "ok": False,
            "error": "Не удалось создать план -- возможно, по этой бумаге в этой стратегии уже есть активный план.",
            "listing_id": listing_id,
        }
    pipeline_id = result[0]["id"] if isinstance(result, list) else result["id"]

    db_instance.ensure_watchlist_row_v2(portfolio_id=portfolio_id, listing_id=listing_id, reason="watched")

    logging.info(f"✅ [DealPlanner]: План входа #{pipeline_id} создан ({symbol}, {qty} шт, портфель {portfolio_id}, шпаргалка={cheat_sheet}).")
    return {"ok": True, "pipeline_id": pipeline_id, "symbol": symbol, "qty": qty, "listing_id": listing_id, "override": override}


def create_sell_plan(db_instance, portfolio_id: int, strategy_id: int, listing_id: int, cheat_sheet: str) -> dict:
    """
    Полный выход -- перепроверяет держащееся количество заново. Заводит План
    (PENDING, шаг 1, ОТРИЦАТЕЛЬНЫЙ target_quantity -- конвенция лесенки продаж,
    см. sync_strategy_asset_fb.py) с выбранной шпаргалкой.
    """
    holding_row = db_instance.execute_row(
        """
            SELECT sa.allocated_quantity, t.id AS ticker_id, t.symbol
            FROM public.strategy_assets sa
            JOIN public.assets a ON a.id = sa.asset_id
            JOIN public.listings l ON l.id = a.listing_id
            JOIN public.tickers t ON t.id = l.ticker_id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.strategy_id = %s AND sa.allocated_quantity > 0;
        """,
        (portfolio_id, listing_id, strategy_id)
    )
    if not holding_row:
        return {"ok": False, "error": "Условия изменились -- позиция уже не держится в этой стратегии."}

    qty = float(holding_row["allocated_quantity"])
    ticker_id = int(holding_row["ticker_id"])
    symbol = holding_row["symbol"]

    override = resolve_cheat_sheet(db_instance, listing_id, "SELL", cheat_sheet)
    if "error" in override:
        return {"ok": False, "error": override["error"]}

    result = db_instance.execute_query(
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, entry_trigger_override, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 1, 'PENDING', %s, 0, NULL, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (portfolio_id, listing_id, ticker_id, strategy_id, -qty, json.dumps(override))
    )
    if not result:
        return {"ok": False, "error": "Не удалось создать план -- возможно, по этой бумаге уже есть активный план."}
    pipeline_id = result[0]["id"] if isinstance(result, list) else result["id"]

    db_instance.ensure_watchlist_row_v2(portfolio_id=portfolio_id, listing_id=listing_id, reason="watched")

    logging.info(f"✅ [DealPlanner]: План выхода #{pipeline_id} создан ({symbol}, {qty:g} шт, портфель {portfolio_id}, шпаргалка={cheat_sheet}).")
    return {"ok": True, "pipeline_id": pipeline_id, "symbol": symbol, "qty": qty, "listing_id": listing_id, "override": override}


def create_trim_plan(db_instance, portfolio_id: int, strategy_id: int, listing_id: int) -> dict:
    """
    Подрезка перевешенной позиции (Claude/13_portfolio_construction_and_rebalancing_rules.md,
    миграция на «К сделке» -- Claude/BACKLOG.md, 2026-08-18) -- ЧАСТИЧНЫЙ выход, в отличие
    от create_sell_plan. Без шпаргалки ASAP/оптимальная цена (согласовано с пользователем --
    подрезка это гигиена портфеля, не решение о моменте входа/выхода) -- всегда рынком.

    Перепроверяет PortfolioRebalancer заново (не по цифрам утреннего дайджеста) -- позиция
    могла перестать быть перевешенной, если цена откатилась. trim_shares -- см.
    analytics_utils.resolve_trim_shares (целые акции, минимум 1 остаётся).
    """
    alerts = PortfolioRebalancer(db_instance).evaluate_portfolio(portfolio_id)
    match = next(
        (a for a in alerts
         if a.get("recommendation") == "TRIM_DOWN" and int(a.get("listing_id", -1)) == listing_id and int(a.get("strategy_id", -1)) == strategy_id),
        None
    )
    if not match:
        return {"ok": False, "error": "Условия изменились -- подрезка больше не актуальна."}

    asset_id = int(match["asset_id"])
    ticker_id = int(match["ticker_id"])
    symbol = match["symbol"]

    asset_row = db_instance.execute_row("SELECT quantity FROM public.assets WHERE id = %s;", (asset_id,))
    held_qty = float((asset_row or {}).get("quantity") or 0.0)
    trim_shares = resolve_trim_shares(float(match["quantity"]), held_qty)
    if trim_shares <= 0:
        return {"ok": False, "error": "Условия изменились -- подрезка больше не актуальна."}

    override = {"mode": "market"}

    result = db_instance.execute_query(
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, entry_trigger_override, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 1, 'PENDING', %s, 0, NULL, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (portfolio_id, listing_id, ticker_id, strategy_id, -trim_shares, json.dumps(override))
    )
    if not result:
        return {"ok": False, "error": "Не удалось создать план -- возможно, по этой бумаге уже есть активный план."}
    pipeline_id = result[0]["id"] if isinstance(result, list) else result["id"]

    db_instance.ensure_watchlist_row_v2(portfolio_id=portfolio_id, listing_id=listing_id, reason="watched")

    logging.info(f"✅ [DealPlanner]: План подрезки #{pipeline_id} создан ({symbol}, {trim_shares} из {held_qty:g} шт, портфель {portfolio_id}).")
    return {"ok": True, "pipeline_id": pipeline_id, "symbol": symbol, "qty": trim_shares, "listing_id": listing_id, "override": override}
