import datetime

import settings
from analytics.volatility_utils import daily_volatility_or_fallback


def _order_reference_price(order: dict) -> float:
    """
    Цена-ориентир приказа -- для stop_loss/take_profit это stop_price, для обычного
    лимитного/рыночного -- p. Та же категоризация, что и в bot_screens.py::classify_order
    (type=5 -> stop_loss, type=6 -> take_profit), продублирована здесь намеренно узко
    (analytics/ не заходит за UI-хелперами в bot_handlers/, кроме лёгкого MenuAction).
    """
    order_type = int(order.get("type") or 0)
    if order_type in (5, 6):
        return float(order.get("stop_price") or order.get("p") or 0)
    return float(order.get("p") or 0)


def _age_days(created_at) -> int:
    if not created_at:
        return 0
    if isinstance(created_at, str):
        created_at = datetime.datetime.fromisoformat(created_at)
    d = created_at.date() if isinstance(created_at, datetime.datetime) else created_at
    return (datetime.date.today() - d).days


def _price_stale_threshold_pct(daily_volatility_pct) -> float:
    """Ось 1 (цена) -- см. Claude/BACKLOG.md, 2026-07-30."""
    return daily_volatility_or_fallback(daily_volatility_pct) * settings.STALE_PRICE_VOLATILITY_MULTIPLIER


def is_broker_order_price_stale(db_instance, broker_order_id) -> bool:
    """
    Точечная проверка ОДНОГО приказа по broker_order_id (ось 1, без времени) -- для
    вкладки "План" карточки тикера (bot_handlers/tickers.py), где нужен статус только
    привязанного приказа, не всего портфеля. Та же формула, что и в
    evaluate_portfolio_staleness, просто на одну строку вместо пакета.
    """
    safe_id = str(broker_order_id).replace("'", "''")
    row = db_instance.execute_row(f"""
        SELECT o.p, o.stop_price, o.type, l.last_price, t.signal_daily_volatility_pct
        FROM public.orders o
        JOIN public.listings l ON o.listing_id = l.id
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE o.broker_order_id = '{safe_id}' LIMIT 1;
    """)
    if not row:
        return False
    ref_price = _order_reference_price(row)
    current_price = float(row.get("last_price") or 0)
    if ref_price <= 0 or current_price <= 0:
        return False
    gap_pct = abs(current_price - ref_price) / ref_price * 100.0
    return gap_pct > _price_stale_threshold_pct(row.get("signal_daily_volatility_pct"))


def evaluate_portfolio_staleness(db_instance, portfolio_id: int) -> dict:
    """
    Единый оценщик протухания приказов И алертов (заменяет узкий analytics/stale_order_watcher.py,
    который проверял только приказы внутри Плана) -- см. Claude/BACKLOG.md, п.5, 2026-07-30.
    Чистая функция без побочных эффектов (никакого Telegram/UPDATE) -- пересчитывается
    заново на каждый вызов, как PositionExitEvaluator/CashDeploymentAdvisor, вызывается
    из analytics/daily_digest.py.

    Ось 1 (цена, без времени) -- пересчитывается с нуля каждый раз, не хранит "уже
    уведомляли": |текущая − цена_приказа| / цена_приказа > волатильность_бумаги × K'.
    Ось 2 (время) -- висит без движения дольше STALE_ORDER_TIME_LIMIT_DAYS, независимо
    от цены.

    Возвращает {"price": [...], "time": [...]} -- каждый пункт содержит "kind"
    ("order"/"alert"), "symbol", "listing_id" и специфичные для оси поля.
    """
    price_items = []
    time_items = []

    order_rows = db_instance.execute_query(f"""
        SELECT o.id AS order_id, o.p, o.stop_price, o.type, o.created_at,
               l.id AS listing_id, l.last_price, t.symbol, t.signal_daily_volatility_pct
        FROM public.orders o
        JOIN public.listings l ON o.listing_id = l.id
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE o.portfolio_id = {int(portfolio_id)}
          AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
    """)
    order_rows = order_rows if isinstance(order_rows, list) else ([order_rows] if order_rows else [])

    for o in order_rows:
        ref_price = _order_reference_price(o)
        current_price = float(o.get("last_price") or 0)
        symbol = o["symbol"]
        listing_id = int(o["listing_id"])

        if ref_price > 0 and current_price > 0:
            gap_pct = abs(current_price - ref_price) / ref_price * 100.0
            threshold_pct = _price_stale_threshold_pct(o.get("signal_daily_volatility_pct"))
            if gap_pct > threshold_pct:
                price_items.append({
                    "kind": "order", "symbol": symbol, "listing_id": listing_id,
                    "order_price": ref_price, "current_price": current_price, "gap_pct": round(gap_pct, 1),
                })

        age = _age_days(o.get("created_at"))
        if age > settings.STALE_ORDER_TIME_LIMIT_DAYS:
            time_items.append({"kind": "order", "symbol": symbol, "listing_id": listing_id, "age_days": age})

    alert_rows = db_instance.execute_query(f"""
        SELECT al.id AS alert_id, al.trigger_price, al.created_at,
               l.id AS listing_id, l.last_price, t.symbol, t.signal_daily_volatility_pct
        FROM public.alerts al
        JOIN public.listings l ON al.listing_id = l.id
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE al.portfolio_id = {int(portfolio_id)} AND al.is_active = true
          AND al.trigger_price IS NOT NULL;
    """)
    alert_rows = alert_rows if isinstance(alert_rows, list) else ([alert_rows] if alert_rows else [])

    for a in alert_rows:
        trigger_price = float(a.get("trigger_price") or 0)
        current_price = float(a.get("last_price") or 0)
        symbol = a["symbol"]
        listing_id = int(a["listing_id"])

        if trigger_price > 0 and current_price > 0:
            gap_pct = abs(current_price - trigger_price) / trigger_price * 100.0
            threshold_pct = _price_stale_threshold_pct(a.get("signal_daily_volatility_pct"))
            if gap_pct > threshold_pct:
                price_items.append({
                    "kind": "alert", "symbol": symbol, "listing_id": listing_id,
                    "order_price": trigger_price, "current_price": current_price, "gap_pct": round(gap_pct, 1),
                })

        age = _age_days(a.get("created_at"))
        if age > settings.STALE_ORDER_TIME_LIMIT_DAYS:
            time_items.append({"kind": "alert", "symbol": symbol, "listing_id": listing_id, "age_days": age})

    return {"price": price_items, "time": time_items}
