import logging

from brokers_connectors.fb_client import FreedomBrokerClient

"""
Единый отчёт по комиссии для ЛЮБОГО портфеля (Claude/BACKLOG.md №88, реализовано
2026-09-06, продолжение обсуждения принципов при разборе живой сделки COPX.US/П136).

Источник данных принципиально разный по типу портфеля (см. add_commission_config.py
и обсуждение в BACKLOG) -- это НЕ произвол, а прямое следствие принципа "снэпшот, не
архив" (CLAUDE.md): реальная комиссия -- брокерский факт, не хранится, спрашивается
на лету; бумажная комиссия -- наше собственное решение, нигде больше не появляется,
если не посчитать и не записать самим (эмулятор -- brokers_connectors/paper_broker.py).

Единая ФОРМА ответа скрывает эту разницу от потребителя (будущий экран/аналитика
"честная доходность за вычетом издержек", направление 9 роадмапа) -- источник
разный, отчёт одинаковый.
"""


def get_commission_summary(db_instance, portfolio_id: int, start_date: str, end_date: str) -> dict:
    """
    start_date/end_date -- 'YYYY-MM-DD'. Возвращает:
    {"portfolio_id", "source": "BROKER"|"PAPER", "total_commission_usd", "trades": [...]}
    trades -- список сделок с полями ticker/qty/price/commission_usd (форма единая,
    даже если источник разный -- реальные поля НЕ хранятся, PAPER читаются из orders).
    """
    portfolio = db_instance.execute_row(
        "SELECT broker_id, owner_id FROM public.portfolios WHERE id = %s;", (portfolio_id,)
    )
    if not portfolio:
        return {"portfolio_id": portfolio_id, "source": None, "total_commission_usd": 0.0, "trades": [],
                "error": "Портфель не найден."}

    if portfolio.get("broker_id") is not None:
        return _get_real_commission(db_instance, portfolio_id, int(portfolio["owner_id"]), start_date, end_date)
    return _get_paper_commission(db_instance, portfolio_id, start_date, end_date)


def _get_real_commission(db_instance, portfolio_id: int, owner_id: int, start_date: str, end_date: str) -> dict:
    client = FreedomBrokerClient.create_for_user(owner_id, db_instance)
    if not client:
        return {"portfolio_id": portfolio_id, "source": "BROKER", "total_commission_usd": 0.0, "trades": [],
                "error": "Не удалось создать клиента брокера (нет ключей/пользователя)."}

    try:
        raw_trades = client.get_trades_history(start_date, end_date)
    except Exception as e:
        logging.error(f"❌ [CommissionReport]: Сбой запроса getTradesHistory (портфель {portfolio_id}): {e}")
        return {"portfolio_id": portfolio_id, "source": "BROKER", "total_commission_usd": 0.0, "trades": [],
                "error": f"Сбой запроса к брокеру: {e}"}

    trades = [{
        "ticker": t["ticker"], "qty": t["qty"], "price": t["price"],
        "commission_usd": t["commission_usd"], "executed_at": t["executed_at"],
    } for t in raw_trades]
    total = sum(t["commission_usd"] for t in trades)
    return {"portfolio_id": portfolio_id, "source": "BROKER", "total_commission_usd": round(total, 2), "trades": trades}


def _get_paper_commission(db_instance, portfolio_id: int, start_date: str, end_date: str) -> dict:
    rows = db_instance.execute_query("""
        SELECT t.symbol AS ticker, o.q AS qty, o.p AS price, o.commission_usd, o.created_at AS executed_at
        FROM public.orders o
        JOIN public.tickers t ON t.id = o.ticker_id
        WHERE o.portfolio_id = %s AND o.commission_usd IS NOT NULL
          AND o.created_at::date BETWEEN %s::date AND %s::date
        ORDER BY o.created_at;
    """, (portfolio_id, start_date, end_date))
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    trades = [{
        "ticker": r["ticker"], "qty": float(r["qty"]), "price": float(r["price"]),
        "commission_usd": float(r["commission_usd"]), "executed_at": str(r["executed_at"]),
    } for r in rows]
    total = sum(t["commission_usd"] for t in trades)
    return {"portfolio_id": portfolio_id, "source": "PAPER", "total_commission_usd": round(total, 2), "trades": trades}
