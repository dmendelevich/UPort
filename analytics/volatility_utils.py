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
