import logging

import settings
from analytics.price_move_watcher import send_alert_notification
from analytics.volatility_utils import portfolio_daily_volatility_or_fallback


def check_portfolio_drawdown(db_instance):
    """
    Сигнал D -- портфельный трейлинг-стоп прибыли (см. Claude/23_session_followups_2026-08-20.md).
    В отличие от сигнала B (capital_protection_watcher.py, трейлинг по КОНКРЕТНОЙ позиции
    от avg_price/peak_price бумаги) -- здесь пик и просадка считаются по total_capital
    ВСЕГО портфеля (public.portfolio_total_capital, вычислительный view). Индексное ядро
    входит внутрь total_capital, не вычитается отдельно (согласовано явно) -- если оба
    сигнала (C -- обвал рынка, D -- этот) горят разом, дело в рынке, если только D --
    дело в активных ставках.

    Пик -- all-time high-water mark с начала жизни портфеля (portfolios.peak_total_capital_usd),
    обновляется каждый цикл, если текущее значение выше -- та же механика, что
    assets.peak_price_since_entry у сигнала B, просто на уровне портфеля.

    Порог = K × дневная волатильность NAV (PORTFOLIO_DRAWDOWN_TRAILING_K, settings.py),
    БЕЗ временной составляющей -- тот же принцип, что у сигнала B (см. докстринг
    check_capital_protection), не "движение за окно", а "насколько ненормальна просадка
    от пика независимо от срока".

    Реакция -- чисто информационный пуш, БЕЗ кнопки действия: под одной цифрой total_capital
    нет одной бумаги, которую можно предложить продать (в отличие от сигнала B).

    Состояние алерта хранится прямо на portfolios (drawdown_alert_active/_triggered_at),
    НЕ через public.alerts -- та таблица требует NOT NULL listing_id/ticker (заточена
    под конкретную бумагу), портфельное условие туда не ложится честно (обсуждено и
    решено в сессии 2026-08-21, вариант 1 из трёх).

    Известное ограничение v1: система не учитывает переводы денег на/со счёта брокера
    отдельно от результата стратегии -- крупное снятие кэша будет выглядеть как просадка
    и может ложно взвести сигнал. Осознанно не обрабатывается (переводы не планируются
    в ближайшее время); реакция всё равно только информационная, ложное срабатывание не
    критично.

    Вызывается из того же цикла котировок, что и сигналы A/B (sync_quotes_fb.py).
    """
    portfolios = db_instance.execute_query("""
        SELECT p.id AS portfolio_id, p.name, p.peak_total_capital_usd, p.nav_daily_volatility_pct,
               p.drawdown_alert_active, p.drawdown_alert_triggered_at,
               ptc.total_capital_usd, u.telegram_id
        FROM public.portfolios p
        JOIN public.portfolio_total_capital ptc ON ptc.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        WHERE ptc.total_capital_usd > 0;
    """)
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    for p in portfolios:
        try:
            _check_one_portfolio(db_instance, p)
        except Exception as err:
            logging.error(f"⚠️ [PortfolioDrawdown]: Сбой проверки портфеля {p.get('portfolio_id')}: {err}")


def _check_one_portfolio(db_instance, p: dict):
    portfolio_id = int(p["portfolio_id"])
    current_total = float(p["total_capital_usd"])

    # Бегущий пик обновляется всегда, независимо от того, сработает ли что-то ниже --
    # см. тот же принцип у capital_protection_watcher.py::_check_one_position.
    stored_peak = float(p.get("peak_total_capital_usd") or current_total)
    new_peak = max(stored_peak, current_total)
    if new_peak != stored_peak:
        db_instance.execute_query(
            "UPDATE public.portfolios SET peak_total_capital_usd = %s WHERE id = %s;",
            (new_peak, portfolio_id)
        )

    daily_vol = portfolio_daily_volatility_or_fallback(p.get("nav_daily_volatility_pct"))
    trigger_total = new_peak * (1 - settings.PORTFOLIO_DRAWDOWN_TRAILING_K * daily_vol / 100.0)

    if current_total <= trigger_total:
        _handle_triggered(db_instance, p, current_total, new_peak)
    elif p.get("drawdown_alert_active"):
        db_instance.execute_query(
            "UPDATE public.portfolios SET drawdown_alert_active = false WHERE id = %s;",
            (portfolio_id,)
        )


def _handle_triggered(db_instance, p: dict, current_total: float, peak_total: float):
    portfolio_id = int(p["portfolio_id"])

    if p.get("drawdown_alert_active"):
        row = db_instance.execute_row("""
            SELECT EXTRACT(EPOCH FROM (
                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - drawdown_alert_triggered_at
            ))::int AS seconds_since
            FROM public.portfolios WHERE id = %s;
        """, (portfolio_id,))
        seconds_since = int((row or {}).get("seconds_since") or 0)
        if seconds_since < settings.PORTFOLIO_DRAWDOWN_PERIODIC_SEC:
            return

    db_instance.execute_query("""
        UPDATE public.portfolios
        SET drawdown_alert_active = true,
            drawdown_alert_triggered_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
        WHERE id = %s;
    """, (portfolio_id,))

    pct = (current_total - peak_total) / peak_total * 100.0
    note = (
        f"📉 Просадка портфеля «{p['name']}»: {pct:+.1f}% от пика "
        f"(${peak_total:,.2f} → ${current_total:,.2f})."
    )
    logging.info(f"🎯 [PortfolioDrawdown]: Портфель {portfolio_id} ({p['name']}) -- просадка {pct:.1f}% от пика капитала.")

    send_alert_notification(p.get("telegram_id"), note)
