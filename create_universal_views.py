#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Порядок важен: сначала базовый слой, затем слой владения/наблюдения (зависит от
# v_listings_tickers), затем состав стратегий (зависит от v_assets_full).
DROP_STATEMENTS = [
    "DROP VIEW IF EXISTS public.v_strategy_assets_full CASCADE;",
    "DROP VIEW IF EXISTS public.v_order_pipelines_full CASCADE;",
    "DROP VIEW IF EXISTS public.v_orders_full CASCADE;",
    "DROP VIEW IF EXISTS public.v_watchlist_full CASCADE;",
    "DROP VIEW IF EXISTS public.v_assets_full CASCADE;",
    "DROP VIEW IF EXISTS public.v_listings_tickers CASCADE;",
]

CREATE_STATEMENTS = [
    # Слой 0: одна строка = один листинг бумаги у брокера, обогащённый всеми полями tickers.
    # t.* тянется целиком, чтобы view не требовал правки при изменении набора колонок tickers
    # (в т.ч. при будущей чистке "чёрного списка" колонок).
    """
    CREATE VIEW public.v_listings_tickers AS
    SELECT
        l.id AS listing_id,
        l.broker_id,
        l.broker_symbol,
        l.currency_id AS listing_currency_id,
        l.last_price AS listing_last_price,
        l.last_updated_at AS listing_last_updated_at,
        t.*
    FROM public.listings l
    JOIN public.tickers t ON l.ticker_id = t.id;
    """,

    # Слой 1а: одна строка = одна реальная позиция (холдинг) в портфеле.
    """
    CREATE VIEW public.v_assets_full AS
    SELECT
        a.id AS asset_id,
        a.quantity,
        a.avg_price,
        a.currency_id AS asset_currency_id,
        a.position_opened_at,
        a.last_updated AS asset_last_updated,
        p.id AS portfolio_id,
        p.name AS portfolio_name,
        p.strategy_type AS portfolio_strategy_type,
        p.risk_profile AS portfolio_risk_profile,
        u.id AS owner_id,
        u.name AS owner_name,
        u.role AS owner_role,
        lt.*
    FROM public.assets a
    JOIN public.portfolios p ON a.portfolio_id = p.id
    JOIN public.users u ON p.owner_id = u.id
    JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id;
    """,

    # Слой 1б: одна строка = одна запись вотчлиста. active_alerts_count через скалярный
    # подзапрос, а не GROUP BY -- иначе пришлось бы перечислять все колонки lt.* в группировке.
    """
    CREATE VIEW public.v_watchlist_full AS
    SELECT
        w.id AS watchlist_id,
        w.portfolio_id,
        w.updated_at AS watchlist_updated_at,
        w.last_interest_at,
        w.watched_at,
        w.ordered_at,
        w.bought_at,
        w.sold_out_at,
        w.evaluation_cache,
        lt.*,
        (
            SELECT COUNT(*)
            FROM public.alerts al
            WHERE al.listing_id = w.listing_id
              AND al.portfolio_id = w.portfolio_id
              AND al.is_active
        ) AS active_alerts_count
    FROM public.watchlist w
    JOIN public.v_listings_tickers lt ON w.listing_id = lt.listing_id;
    """,

    # Слой 1в: одна строка = один ордер.
    """
    CREATE VIEW public.v_orders_full AS
    SELECT
        o.id AS order_id,
        o.portfolio_id,
        o.broker_order_id,
        o.status AS order_status,
        o.created_at AS order_created_at,
        o.currency_id AS order_currency_id,
        o.oper,
        o.type AS order_type,
        o.q AS order_quantity,
        o.p AS order_price,
        o.stop_init_price,
        o.stop_price,
        p.name AS portfolio_name,
        lt.*
    FROM public.orders o
    JOIN public.portfolios p ON o.portfolio_id = p.id
    JOIN public.v_listings_tickers lt ON o.listing_id = lt.listing_id;
    """,

    # Слой 1г: одна строка = один шаг пайплайна исполнения ордера по стратегии.
    """
    CREATE VIEW public.v_order_pipelines_full AS
    SELECT
        op.id AS pipeline_id,
        op.portfolio_id,
        op.strategy_id,
        op.current_step,
        op.pipeline_status,
        op.target_quantity,
        op.split_adjustment_factor,
        op.initial_entry_price,
        op.created_at AS pipeline_created_at,
        op.updated_at AS pipeline_updated_at,
        s.strategy_name,
        p.name AS portfolio_name,
        lt.*
    FROM public.order_pipelines op
    JOIN public.strategies s ON op.strategy_id = s.id
    JOIN public.portfolios p ON op.portfolio_id = p.id
    JOIN public.v_listings_tickers lt ON op.listing_id = lt.listing_id;
    """,

    # Слой 2: одна строка = доля одной позиции, закреплённая за одной стратегией.
    """
    CREATE VIEW public.v_strategy_assets_full AS
    SELECT
        sa.id AS strategy_asset_id,
        sa.allocated_quantity,
        sa.expected_quantity,
        sa.last_updated_at AS strategy_asset_last_updated_at,
        s.id AS strategy_id,
        s.strategy_name,
        s.rules_config,
        s.strategy_share_pct,
        s.is_active AS strategy_is_active,
        va.*
    FROM public.strategy_assets sa
    JOIN public.strategies s ON sa.strategy_id = s.id
    JOIN public.v_assets_full va ON sa.asset_id = va.asset_id;
    """,
]

COMMENTS = [
    "COMMENT ON VIEW public.v_listings_tickers IS 'Слой 0: один листинг бумаги у брокера + все поля tickers. Базовый view для остальных.';",
    "COMMENT ON VIEW public.v_assets_full IS 'Слой 1: одна реальная позиция (холдинг) в портфеле, с полным контекстом бумаги и владельца.';",
    "COMMENT ON VIEW public.v_watchlist_full IS 'Слой 1: одна запись вотчлиста портфеля, с полным контекстом бумаги.';",
    "COMMENT ON VIEW public.v_orders_full IS 'Слой 1: один ордер, с полным контекстом бумаги.';",
    "COMMENT ON VIEW public.v_order_pipelines_full IS 'Слой 1: один шаг пайплайна исполнения по стратегии, с полным контекстом бумаги.';",
    "COMMENT ON VIEW public.v_strategy_assets_full IS 'Слой 2: доля одной позиции портфеля, закреплённая за стратегией.';",
]


def run():
    logging.info("Удаление старых версий view (если есть)...")
    for stmt in DROP_STATEMENTS:
        db_sys.execute_query(stmt)

    logging.info("Создание view...")
    for stmt in CREATE_STATEMENTS:
        res = db_sys.execute_query(stmt)
        if not res or res[0].get("status") != "success":
            logging.error(f"Сбой при выполнении: {stmt[:80]}...")
            return False

    logging.info("Простановка комментариев...")
    for stmt in COMMENTS:
        db_sys.execute_query(stmt)

    logging.info("Готово.")
    return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
