import statistics

import settings


def daily_volatility_or_fallback(daily_volatility_pct) -> float:
    """
    Общий примитив волатильности (см. Claude/BACKLOG.md, 2026-07-30) -- возвращает
    tickers.signal_daily_volatility_pct, либо запасной плоский порог
    (PRICE_MOVE_WATCHER_THRESHOLD_PCT), если у бумаги ещё нет ночной истории
    (напр. только что добавленный тикер). Используется всеми потребителями
    (PriceMoveWatcher, протухание приказов/алертов), чтобы не дублировать откат.
    """
    if daily_volatility_pct is not None:
        return float(daily_volatility_pct)
    return settings.PRICE_MOVE_WATCHER_THRESHOLD_PCT


def portfolio_daily_volatility_or_fallback(nav_daily_volatility_pct) -> float:
    """
    Симметрично daily_volatility_or_fallback выше, но для NAV портфеля (Сигнал D,
    Claude/23_session_followups_2026-08-20.md) -- возвращает portfolios.
    nav_daily_volatility_pct, либо запасной плоский порог (PORTFOLIO_NAV_VOLATILITY_
    FALLBACK_PCT), если у портфеля ещё нет истории снимков (только создан) или истории
    мало (см. compute_portfolio_nav_volatility).
    """
    if nav_daily_volatility_pct is not None:
        return float(nav_daily_volatility_pct)
    return settings.PORTFOLIO_NAV_VOLATILITY_FALLBACK_PCT


def compute_portfolio_nav_volatility(db_instance, portfolio_id: int):
    """
    Дневная волатильность NAV портфеля -- std дневных % изменений total_value по
    portfolio_value_history, тот же принцип, что и tickers.signal_daily_volatility_pct
    (sync_signals_yf.py: std дневных % изменений цены за окно). Возвращает None, если
    истории меньше PORTFOLIO_NAV_VOLATILITY_MIN_HISTORY_DAYS точек -- вызывающий код
    сам решает про фолбэк (portfolio_daily_volatility_or_fallback выше), эта функция
    оценку "на скорую руку" не гадает.
    """
    rows = db_instance.execute_query("""
        SELECT total_value FROM public.portfolio_value_history
        WHERE portfolio_id = %s
        ORDER BY snapshot_date DESC
        LIMIT %s;
    """, (portfolio_id, settings.PORTFOLIO_NAV_VOLATILITY_WINDOW_DAYS))
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])
    if len(rows) < settings.PORTFOLIO_NAV_VOLATILITY_MIN_HISTORY_DAYS:
        return None

    values = [float(r["total_value"]) for r in reversed(rows)]
    returns = [
        (values[i] - values[i - 1]) / values[i - 1] * 100.0
        for i in range(1, len(values)) if values[i - 1]
    ]
    if len(returns) < 2:
        return None
    return round(statistics.stdev(returns), 4)
