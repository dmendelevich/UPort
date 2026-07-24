import logging
import os
import requests

STALE_DRIFT_PCT = 5.0
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def check_stale_pending_orders(db_instance):
    """
    Проверяет все активные привязанные ордера (order_pipelines.pending_broker_order_id)
    на расхождение с текущей котировкой (>5% -- см. Claude/09_pipeline_reconciliation.md).
    Уведомляет РАЗОВО при первом пересечении порога (не на каждом цикле котировок);
    если цена вернулась в норму -- снимает отметку, чтобы повторное расхождение снова
    дало уведомление. Вызывается из sync_quotes_fb_autonomous() после обновления котировок.
    """
    sql = """
        SELECT op.id AS pipeline_id, op.stale_notified_at, o.p AS order_price,
               l.last_price, t.symbol, s.strategy_name, port.name AS portfolio_name, u.telegram_id
        FROM public.order_pipelines op
        JOIN public.orders o ON o.broker_order_id = op.pending_broker_order_id
        JOIN public.listings l ON l.id = op.listing_id
        JOIN public.tickers t ON t.id = op.ticker_id
        JOIN public.strategies s ON s.id = op.strategy_id
        JOIN public.portfolios port ON port.id = op.portfolio_id
        JOIN public.users u ON u.id = port.owner_id
        WHERE op.pipeline_status IN ('PENDING', 'ACTIVE')
          AND op.pending_broker_order_id IS NOT NULL;
    """
    rows = db_instance.execute_query(sql)
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    for row in rows:
        try:
            _check_row(db_instance, row)
        except Exception as row_err:
            logging.error(f"⚠️ [StaleOrderWatcher]: Сбой проверки протухания (pipeline_id={row.get('pipeline_id')}): {row_err}")


def _check_row(db_instance, row):
    order_price = float(row.get("order_price") or 0)
    last_price = float(row.get("last_price") or 0)
    if order_price <= 0 or last_price <= 0:
        return

    drift_pct = abs(last_price - order_price) / order_price * 100.0
    pipeline_id = int(row["pipeline_id"])
    already_notified = row.get("stale_notified_at") is not None

    if drift_pct > STALE_DRIFT_PCT:
        if already_notified:
            return  # уже уведомляли, ждём либо исполнения, либо возврата в норму
        db_instance.execute_query(
            f"UPDATE public.order_pipelines SET stale_notified_at = CURRENT_TIMESTAMP WHERE id = {pipeline_id};"
        )
        _send_notification(row, drift_pct)
        logging.info(f"⚠️ [StaleOrderWatcher]: Ордер по плану #{pipeline_id} протух (расхождение {drift_pct:.1f}%), уведомление отправлено.")
    else:
        if already_notified:
            db_instance.execute_query(
                f"UPDATE public.order_pipelines SET stale_notified_at = NULL WHERE id = {pipeline_id};"
            )


def _send_notification(row, drift_pct):
    telegram_id = row.get("telegram_id")
    if not telegram_id:
        return

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logging.warning("⚠️ [StaleOrderWatcher]: TELEGRAM_TOKEN отсутствует -- уведомление о протухании не отправлено.")
        return

    text = (
        f"⚠️ Приказ по *{row['symbol']}* ({row['strategy_name']}, портфель {row['portfolio_name']}) "
        f"устарел: цена ушла на {drift_pct:.1f}% от цены приказа ({float(row['order_price']):.2f} → "
        f"{float(row['last_price']):.2f}). Пересмотрите или отмените приказ."
    )
    try:
        requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": int(telegram_id), "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as send_err:
        logging.error(f"⚠️ [StaleOrderWatcher]: Не удалось отправить уведомление о протухании: {send_err}")
