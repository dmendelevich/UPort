import json
import logging
from datetime import datetime, timezone

from analytics.deal_planner import create_sell_plan
from analytics.portfolio_inspector import PortfolioInspector

"""
Интерфейс дискреционных сделок «ПБумКлод» (execution_mode='ADVISORY', portfolio_id=20,
Claude/BACKLOG.md, 2026-08-29, тема «Мандат ПБумКлод»). Вызывается напрямую (Python/
db_sys), не через Telegram -- решения принимает агент при собственном пробуждении,
не человек кликами. Мандат: любые акции/фонды США (не только из `tickers` -- легализуются
на лету через ensure_ticker_v3, тот же путь, что и ручной ввод тикера человеком),
лимиты риска -- уже действующие дефолты portfolio_max_asset_pct/sector_pct=5/25%
(PortfolioInspector.audit_limits_and_rules, самому проверять ПЕРЕД размером позиции --
это не блокирующий гейт, а отчёт), выход -- целиком на суждении агента, без
механического бэкстопа (CapitalProtectionWatcher для этого портфеля не настроен).

Покупка -- НЕ через analytics/deal_planner.py::create_buy_plan (та требует
CashDeploymentAdvisor.verify_buy_candidate, а для «Неопределённая» нет функции
скоринга вообще, гарантированно вернёт отказ) -- тот же приём прямой записи в
order_pipelines, что уже применяет bot_handlers/ticker_search.py для «Купить вне
стратегии» (mode='market', решение принято целиком в момент вызова).

Продажа -- ЧЕРЕЗ create_sell_plan (она не завязана на скоринг стратегии, только на
факт удержания в strategy_assets -- работает для «Неопределённая» без изменений).
"""

UNALLOCATED_SYSTEM_KEY = "UNALLOCATED"
CLAUDE_PORTFOLIO_ID = 20
CLAUDE_OWNER_USER_ID = 1


def _system_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")


def _get_unallocated_strategy_id(db_instance, portfolio_id: int) -> int:
    row = db_instance.execute_row("""
        SELECT s.id FROM public.strategies s
        JOIN public.strategy_templates tpl ON s.template_id = tpl.id
        WHERE s.portfolio_id = %s AND tpl.system_key = %s;
    """, (portfolio_id, UNALLOCATED_SYSTEM_KEY))
    if not row:
        raise ValueError(f"В портфеле {portfolio_id} нет буферной стратегии «Неопределённая».")
    return int(row["id"])


def report(db_instance, portfolio_id: int = CLAUDE_PORTFOLIO_ID) -> dict:
    """
    Снимок для агента перед принятием решений: держимые позиции + активный тезис
    по каждой, свободный кэш, живой отчёт по лимитам (самопроверка ПЕРЕД сделкой,
    не после -- согласовано с пользователем, аудитор сам ничего не блокирует).
    """
    cash_row = db_instance.execute_row(
        "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';",
        (portfolio_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)

    holdings = db_instance.execute_query("""
        SELECT a.listing_id, t.id AS ticker_id, t.symbol, a.quantity, a.avg_price,
               l.last_price, th.thesis, th.exit_criteria, th.created_at AS thesis_created_at
        FROM public.assets a
        JOIN public.strategy_assets sa ON sa.asset_id = a.id
        JOIN public.strategies s ON s.id = sa.strategy_id
        JOIN public.strategy_templates tpl ON tpl.id = s.template_id
        JOIN public.listings l ON l.id = a.listing_id
        JOIN public.tickers t ON t.id = l.ticker_id
        LEFT JOIN public.claude_position_thesis th
            ON th.portfolio_id = a.portfolio_id AND th.listing_id = a.listing_id AND th.closed_at IS NULL
        WHERE a.portfolio_id = %s AND tpl.system_key = %s AND sa.allocated_quantity > 0;
    """, (portfolio_id, UNALLOCATED_SYSTEM_KEY))
    holdings = holdings if isinstance(holdings, list) else ([holdings] if holdings else [])

    # Живая находка 2026-09-03: report() показывал только УЖЕ исполненные позиции
    # (assets) -- планы, созданные тем же buy()/sell() в предыдущей сессии, но ещё не
    # исполненные paper_broker.py (рынок был закрыт), были не видны агенту вообще.
    # Итог -- повторная попытка купить ту же бумагу упиралась в SQL-ошибку уникального
    # индекса order_pipelines_unique_active_run вместо чистой проверки заранее. Тот же
    # фикс, что и в bot_handlers/bot_screens.py::format_portfolio_header (BACKLOG №165).
    pending_orders = db_instance.execute_query("""
        SELECT t.symbol, op.target_quantity, op.initial_entry_price
        FROM public.order_pipelines op
        JOIN public.tickers t ON t.id = op.ticker_id
        WHERE op.portfolio_id = %s AND op.pipeline_status IN ('PENDING', 'ACTIVE')
          AND op.entry_trigger_override->>'mode' = 'market';
    """, (portfolio_id,))
    pending_orders = pending_orders if isinstance(pending_orders, list) else ([pending_orders] if pending_orders else [])

    audit = PortfolioInspector(db_instance, portfolio_id).audit_limits_and_rules()

    return {"cash_available": cash_available, "holdings": holdings, "pending_orders": pending_orders, "limits_audit": audit}


def buy(db_instance, ticker_symbol_or_id, amount_usd: float, thesis: str, exit_criteria: str = None,
        portfolio_id: int = CLAUDE_PORTFOLIO_ID) -> dict:
    """
    Легализует тикер при необходимости (ensure_ticker_v3, caller_role='TG_USR' --
    тот же путь, что и ручной ввод человеком, включая ночной синк сигналов, который
    ищет тикеры именно по этой метке provenance -- изобретать новую роль означало
    бы, что RSI/MACD по моим покупкам никогда не обновлялись бы), заводит План
    (PENDING, mode=market) под «Неопределённая», записывает обязательный тезис.

    thesis -- ОБЯЗАТЕЛЕН (согласовано с пользователем 2026-08-29). Возвращает
    {"ok": True, "pipeline_id", "symbol", "qty", "listing_id"} или {"ok": False, "error"}.
    """
    if not thesis or not str(thesis).strip():
        return {"ok": False, "error": "Тезис обязателен -- без него сделка не создаётся."}

    if isinstance(ticker_symbol_or_id, int):
        ticker_id = ticker_symbol_or_id
        listing_row = db_instance.execute_row(
            "SELECT id, last_price FROM public.listings WHERE ticker_id = %s AND broker_id = 1;", (ticker_id,)
        )
        if not listing_row:
            try:
                listing_id = db_instance.ensure_listing(ticker_id, 1)
            except Exception as e:
                return {"ok": False, "error": f"Не удалось легализовать листинг: {e}"}
            listing_row = db_instance.execute_row("SELECT id, last_price FROM public.listings WHERE id = %s;", (listing_id,))
    else:
        try:
            ticker_id, listing_id = db_instance.ensure_ticker_v3(
                ticker_name_raw=str(ticker_symbol_or_id), caller_role="TG_USR",
                caller_id=CLAUDE_OWNER_USER_ID, broker_id=1, fb_client=None
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"Сбой легализации тикера: {e}"}
        # ensure_ticker_v3 легализует ТИКЕР, но не всегда сразу заводит листинг у
        # конкретного брокера (см. _resolve_listing в paper_execution.py -- тот же
        # паттерн) -- новый/редкий тикер может вернуть listing_id=None.
        if not listing_id:
            try:
                listing_id = db_instance.ensure_listing(ticker_id, 1)
            except Exception as e:
                return {"ok": False, "error": f"Не удалось легализовать листинг: {e}"}
        listing_row = db_instance.execute_row("SELECT id, last_price FROM public.listings WHERE id = %s;", (listing_id,))

    listing_id = int(listing_row["id"])
    price = float(listing_row.get("last_price") or 0.0)
    if price <= 0:
        return {"ok": False, "error": "Не удалось получить цену, попробуй позже.", "listing_id": listing_id}

    cash_row = db_instance.execute_row(
        "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';", (portfolio_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)
    if amount_usd > cash_available:
        return {"ok": False, "error": f"Недостаточно кэша: запрошено ${amount_usd:,.2f}, доступно ${cash_available:,.2f}."}

    qty = max(1, round(amount_usd / price))
    s_id = _get_unallocated_strategy_id(db_instance, portfolio_id)

    result = db_instance.execute_query("""
        INSERT INTO public.order_pipelines
            (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
             target_quantity, initial_entry_price, pending_broker_order_id, entry_trigger_override, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 1, 'PENDING', %s, %s, NULL, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id;
    """, (portfolio_id, listing_id, ticker_id, s_id, qty, price, json.dumps({"mode": "market"})))
    if not result:
        return {"ok": False, "error": "Не удалось создать план -- возможно, уже есть активный план по этой бумаге.", "listing_id": listing_id}
    pipeline_id = result[0]["id"] if isinstance(result, list) else result["id"]

    db_instance.ensure_watchlist_row_v2(portfolio_id=portfolio_id, listing_id=listing_id, reason="watched")

    # SELECT-затем-UPDATE/INSERT, не ON CONFLICT (Claude/BACKLOG.md №128).
    now = _system_now()
    existing_thesis = db_instance.execute_row("""
        SELECT id FROM public.claude_position_thesis
        WHERE portfolio_id = %s AND listing_id = %s AND closed_at IS NULL;
    """, (portfolio_id, listing_id))
    if existing_thesis:
        db_instance.execute_query("""
            UPDATE public.claude_position_thesis SET thesis = %s, exit_criteria = %s, updated_at = %s
            WHERE id = %s;
        """, (thesis, exit_criteria, now, int(existing_thesis["id"])))
    else:
        db_instance.execute_query("""
            INSERT INTO public.claude_position_thesis (portfolio_id, listing_id, thesis, exit_criteria, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s);
        """, (portfolio_id, listing_id, thesis, exit_criteria, now, now))

    symbol_row = db_instance.execute_row("SELECT symbol FROM public.tickers WHERE id = %s;", (ticker_id,))
    symbol = (symbol_row or {}).get("symbol", "?")

    logging.info(f"🟢 [ClaudePaperTrader]: План покупки #{pipeline_id} создан ({symbol}, {qty} шт, портфель {portfolio_id}).")
    return {"ok": True, "pipeline_id": pipeline_id, "symbol": symbol, "qty": qty, "listing_id": listing_id}


def sell(db_instance, listing_id: int, close_reason: str, portfolio_id: int = CLAUDE_PORTFOLIO_ID, cheat_sheet: str = "asap") -> dict:
    """
    Полный выход -- через уже существующий create_sell_plan (не завязан на скоринг
    стратегии, работает для «Неопределённая» без изменений). Закрывает тезис
    (closed_at, close_reason) -- строка остаётся архивом, не удаляется.
    """
    s_id = _get_unallocated_strategy_id(db_instance, portfolio_id)
    result = create_sell_plan(db_instance, portfolio_id, s_id, listing_id, cheat_sheet)
    if not result.get("ok"):
        return result

    db_instance.execute_query("""
        UPDATE public.claude_position_thesis
        SET closed_at = %s, close_reason = %s, updated_at = %s
        WHERE portfolio_id = %s AND listing_id = %s AND closed_at IS NULL;
    """, (_system_now(), close_reason, _system_now(), portfolio_id, listing_id))

    logging.info(f"🔴 [ClaudePaperTrader]: План продажи #{result['pipeline_id']} создан ({result['symbol']}, портфель {portfolio_id}).")
    return result
