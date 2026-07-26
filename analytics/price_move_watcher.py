import os
import logging
import requests

import settings
from analytics.quote_snapshot_utils import record_quote_snapshot, compute_price_move
from bot_handlers.common import MenuAction

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _sql_str(v) -> str:
    """Экранирование одинарных кавычек для свободного текста (символ, имя стратегии, заметка)."""
    return f"'{str(v).replace(chr(39), chr(39) + chr(39))}'"


def check_price_moves(db_instance, broker_id: int = 1):
    """
    PriceMoveWatcher (см. Claude/05_strategy_screen_and_kubiki.md): для каждого листинга
    брокера пишет свежую котировку в кольцевой буфер (quote_snapshots, см.
    analytics/quote_snapshot_utils.py), считает % движения цены за
    PRICE_MOVE_WATCHER_WINDOW_MINUTES (settings.py) и, если превышен порог, взводит/
    повторяет алерт (public.alerts, source_type='uport') с уведомлением в Телеграм и
    кнопкой "Остановить". Вызывается из sync_quotes_fb.py после обновления котировок --
    рыночно-зависимая проверка, поэтому на цикле котировок, а не в дайджесте (см. тот же
    принцип в analytics/stale_order_watcher.py, analytics/ladder_step_watcher.py).
    """
    listings = db_instance.execute_query(f"""
        SELECT l.id AS listing_id, l.last_price, t.symbol, t.id AS ticker_id
        FROM public.listings l
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE l.broker_id = {int(broker_id)} AND l.last_price IS NOT NULL AND l.last_price > 0;
    """)
    listings = listings if isinstance(listings, list) else ([listings] if listings else [])

    for row in listings:
        try:
            _check_one_listing(db_instance, row)
        except Exception as err:
            logging.error(f"⚠️ [PriceMoveWatcher]: Сбой проверки листинга {row.get('listing_id')}: {err}")


def _check_one_listing(db_instance, row: dict):
    listing_id = int(row["listing_id"])
    price = float(row["last_price"])
    symbol = row["symbol"]
    ticker_id = int(row["ticker_id"])

    record_quote_snapshot(db_instance, listing_id, price, settings.PRICE_MOVE_WATCHER_BUFFER_SIZE)

    move = compute_price_move(db_instance, listing_id, price, settings.PRICE_MOVE_WATCHER_WINDOW_MINUTES)
    if move is None:
        return  # ещё нет истории на всё окно -- рано считать
    pct_move, actual_minutes = move

    if abs(pct_move) >= settings.PRICE_MOVE_WATCHER_THRESHOLD_PCT:
        direction = "-" if pct_move < 0 else "+"
        trigger_type = "moving_down_from_current" if pct_move < 0 else "moving_up_from_current"
        _handle_triggered(db_instance, listing_id, ticker_id, symbol, price, pct_move, actual_minutes, direction, trigger_type)
    else:
        _resolve_alerts(db_instance, listing_id)


def _get_watching_portfolios(db_instance, listing_id: int) -> list:
    """Портфели, которые реально держат ИЛИ наблюдают этот листинг -- как у брокерских алертов."""
    rows = db_instance.execute_query(f"""
        SELECT DISTINCT p.id AS portfolio_id, p.name AS portfolio_name, u.telegram_id
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        WHERE p.id IN (
            SELECT portfolio_id FROM public.assets WHERE listing_id = {int(listing_id)} AND quantity > 0
            UNION
            SELECT portfolio_id FROM public.watchlist WHERE listing_id = {int(listing_id)}
        );
    """)
    return rows if isinstance(rows, list) else ([rows] if rows else [])


def _get_strategy_names(db_instance, portfolio_id: int, listing_id: int) -> list:
    """
    В какой(-их) стратегии(-ях) этого портфеля лежит бумага -- реальная единица управления
    (см. Claude/05_...), поэтому текст уведомления упоминает стратегию, а не только портфель,
    хотя сама запись алерта остаётся на уровне портфель+листинг (как и брокерские алерты).
    """
    rows = db_instance.execute_query(f"""
        SELECT s.strategy_name FROM public.strategy_assets sa
        JOIN public.assets a ON sa.asset_id = a.id
        JOIN public.strategies s ON sa.strategy_id = s.id
        WHERE a.portfolio_id = {int(portfolio_id)} AND a.listing_id = {int(listing_id)} AND sa.allocated_quantity > 0;
    """)
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])
    return [r["strategy_name"] for r in rows if r.get("strategy_name")]


def _build_note(portfolio_name: str, symbol: str, pct_move: float, actual_minutes: int, strategy_names: list) -> str:
    sign = "+" if pct_move >= 0 else ""
    text = f"{symbol}: {sign}{pct_move:.1f}% за {actual_minutes} мин."
    if strategy_names:
        text += f" В портфеле {portfolio_name}, стратегия «{', '.join(strategy_names)}»."
    else:
        text += f" В портфеле {portfolio_name}."
    text += " Стоит обратить внимание."
    return text


def _handle_triggered(db_instance, listing_id, ticker_id, symbol, price, pct_move, actual_minutes, direction, trigger_type):
    portfolios = _get_watching_portfolios(db_instance, listing_id)
    for p in portfolios:
        portfolio_id = int(p["portfolio_id"])

        existing = db_instance.execute_row(f"""
            SELECT id,
                   EXTRACT(EPOCH FROM ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - triggered_at))::int AS seconds_since
            FROM public.alerts
            WHERE portfolio_id = {portfolio_id} AND listing_id = {int(listing_id)}
              AND source_type = 'uport' AND condition_type = '{direction}' AND is_active = true
            ORDER BY triggered_at DESC LIMIT 1;
        """)

        strategy_names = _get_strategy_names(db_instance, portfolio_id, listing_id)
        note = _build_note(p["portfolio_name"], symbol, pct_move, actual_minutes, strategy_names)

        if existing:
            # Уже взведён -- повторяем уведомление только если прошло periodic секунд
            # (то же поле, что и у брокерских алертов -- не изобретаем параллельный cooldown)
            seconds_since = int(existing.get("seconds_since") or 0)
            if seconds_since < settings.PRICE_MOVE_WATCHER_PERIODIC_SEC:
                continue
            alert_id = int(existing["id"])
            db_instance.execute_query(f"""
                UPDATE public.alerts SET
                    trigger_price = {price}, trigger_pct = {abs(pct_move):.2f},
                    note = {_sql_str(note)},
                    triggered_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                WHERE id = {alert_id};
            """)
        else:
            insert_res = db_instance.execute_query(f"""
                INSERT INTO public.alerts (
                    portfolio_id, listing_id, ticker_id, source_type, ticker, init_price, trigger_price,
                    condition_type, trigger_type, expire_type, periodic, is_active, trigger_pct, note,
                    triggered_at, created_at, updated_at
                ) VALUES (
                    {portfolio_id}, {int(listing_id)}, {int(ticker_id)}, 'uport', {_sql_str(symbol)}, {price}, {price},
                    '{direction}', {_sql_str(trigger_type)}, '0', {settings.PRICE_MOVE_WATCHER_PERIODIC_SEC}, true,
                    {abs(pct_move):.2f}, {_sql_str(note)},
                    (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                    (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                    (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                ) RETURNING id;
            """)
            if not insert_res:
                continue
            alert_id = int(insert_res[0]["id"])
            logging.info(f"🎯 [PriceMoveWatcher]: Новый алерт #{alert_id} для {symbol} (портфель {portfolio_id}, {direction}{abs(pct_move):.1f}%)")

        _send_notification(p.get("telegram_id"), note, alert_id)


def _resolve_alerts(db_instance, listing_id: int):
    """Условие больше не выполняется -- гасим взведённые (обеих сторон) алерты этого листинга."""
    db_instance.execute_query(f"""
        UPDATE public.alerts SET is_active = false, updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
        WHERE listing_id = {int(listing_id)} AND source_type = 'uport' AND is_active = true;
    """)


def _send_notification(telegram_id, note: str, alert_id: int):
    if not telegram_id:
        return

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logging.warning("⚠️ [PriceMoveWatcher]: TELEGRAM_TOKEN отсутствует -- уведомление не отправлено.")
        return

    text = f"📢 {note}"
    stop_callback = MenuAction(action="stop_price_alert", alert_id=int(alert_id)).pack()
    reply_markup = {
        "inline_keyboard": [[{"text": "🛑 Остановить", "callback_data": stop_callback}]]
    }
    try:
        requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": int(telegram_id), "text": text, "reply_markup": reply_markup},
            timeout=10
        )
    except Exception as send_err:
        logging.error(f"⚠️ [PriceMoveWatcher]: Не удалось отправить уведомление: {send_err}")
