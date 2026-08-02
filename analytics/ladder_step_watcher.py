import logging
import os
import requests

import settings
from analytics.analytics_utils import expected_step_quantity

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class LadderStepWatcher:
    """
    Оценка готовности следующей ступени лесенки order_pipelines (см. Claude/09_pipeline_reconciliation.md).
    Для каждого активного плана, у которого текущий шаг завершён, а следующий ордер ещё не привязан
    (pending_broker_order_id IS NULL), заново (не по сохранённому числу) проверяет trigger_conditions
    текущего шага strategy_tactics -- и, если условие выполнено, даёт конкретную цену/объём,
    посчитанные СЕГОДНЯ (listings.last_price), а не когда-то давно.

    Рыночно-зависимо -- поэтому вызывается из цикла обновления котировок (см.
    check_ladder_step_triggers ниже), а не из дайджеста (обсуждено 2026-07-24: дайджест --
    расписание пользователя, а окно возможности зависит от рынка, не от пользователя).
    """

    def __init__(self, db_instance):
        self.db = db_instance

    def evaluate_all(self) -> list:
        """Системная проверка по всем портфелям сразу -- для вызова с цикла котировок."""
        sql = """
            SELECT op.id AS pipeline_id, op.strategy_id, op.current_step, op.target_quantity,
                   op.ticker_id, op.listing_id, op.portfolio_id, op.step_ready_notified_at,
                   t.symbol, s.strategy_name, port.name AS portfolio_name, u.telegram_id,
                   t.signal_rsi, t.signal_macd, t.signal_ema20_streak_days,
                   a.avg_price, l.last_price
            FROM public.order_pipelines op
            JOIN public.tickers t ON op.ticker_id = t.id
            JOIN public.strategies s ON op.strategy_id = s.id
            JOIN public.assets a ON a.portfolio_id = op.portfolio_id AND a.listing_id = op.listing_id
            JOIN public.listings l ON l.id = op.listing_id
            JOIN public.portfolios port ON port.id = op.portfolio_id
            JOIN public.users u ON u.id = port.owner_id
            WHERE op.pipeline_status IN ('PENDING', 'ACTIVE')
              AND op.pending_broker_order_id IS NULL;
        """
        rows = self.db.execute_query(sql)
        rows = rows if isinstance(rows, list) else ([rows] if rows else [])

        results = []
        for row in rows:
            try:
                rec = self._evaluate_row(row)
                results.append({"row": row, "met": rec})
            except Exception as row_err:
                logging.error(f"⚠️ [LadderStepWatcher]: Сбой оценки следующего шага (pipeline_id={row.get('pipeline_id')}): {row_err}")
        return results

    def _evaluate_row(self, row: dict):
        strat_id = int(row["strategy_id"])
        curr_step = int(row["current_step"])

        tactic = self.db.execute_row(f"""
            SELECT budget_share_pct, trigger_conditions
            FROM public.strategy_tactics
            WHERE strategy_id = {strat_id} AND step_number = {curr_step};
        """)
        if not tactic:
            # Шаг не описан в strategy_tactics вообще -- нечего оценивать (см. BACKLOG.md #29/E)
            return None

        conditions = tactic.get("trigger_conditions") or {}
        mode = conditions.get("mode")

        if not conditions or not mode:
            # Черновик стратегии, условие не доопределено (напр. Револьверная, шаг 2) --
            # сознательно не гадаем, тема отнесена к блоку "Аналитика" (BACKLOG.md #29/E)
            return None

        condition_met = False
        if mode == "market":
            condition_met = True
        elif mode == "limit":
            avg_price = float(row.get("avg_price") or 0)
            last_price = float(row.get("last_price") or 0)
            rsi = row.get("signal_rsi")
            max_rsi = conditions.get("max_rsi")
            price_drop_pct = conditions.get("price_drop_pct")

            rsi_ok = (max_rsi is None) or (rsi is not None and float(rsi) <= float(max_rsi))
            price_ok = True
            if price_drop_pct is not None and avg_price > 0:
                threshold_price = avg_price * (1 - float(price_drop_pct) / 100.0)
                price_ok = last_price > 0 and last_price <= threshold_price

            # Подтверждённый разворот (settings.TREND_REVERSAL_CONFIRM_DAYS) -- не только
            # "перепродано и просело" (RSI/price_drop выше), но и "уже видно, что перестало
            # падать", см. Claude/12_investment_goal_and_mechanisms_roadmap.md (MU-кейс).
            reversal_ok = True
            if conditions.get("require_confirmed_reversal"):
                streak = row.get("signal_ema20_streak_days")
                reversal_ok = streak is not None and int(streak) >= settings.TREND_REVERSAL_CONFIRM_DAYS

            condition_met = rsi_ok and price_ok and reversal_ok

        if not condition_met:
            return None

        expected_qty = expected_step_quantity(row["target_quantity"], tactic["budget_share_pct"])
        suggested_price = float(row.get("last_price") or 0)

        return {
            "suggested_qty": expected_qty,
            "suggested_price": suggested_price,
        }


def check_ladder_step_triggers(db_instance):
    """
    Вызывается из цикла обновления котировок (см. sync_quotes_fb.py). Разовое уведомление
    при первом выполнении условия следующего шага (step_ready_notified_at); если условие
    перестало выполняться (цена отскочила обратно) -- снимает флаг, чтобы будущее повторное
    выполнение снова дало уведомление. Дайджест только читает уже проставленный флаг
    (см. analytics/daily_digest.py), сам условие не пересчитывает.
    """
    for item in LadderStepWatcher(db_instance).evaluate_all():
        try:
            _apply_result(db_instance, item["row"], item["met"])
        except Exception as err:
            logging.error(f"⚠️ [LadderStepWatcher]: Сбой обработки результата (pipeline_id={item['row'].get('pipeline_id')}): {err}")


def _apply_result(db_instance, row: dict, met: dict):
    pipeline_id = int(row["pipeline_id"])
    already_notified = row.get("step_ready_notified_at") is not None

    if met:
        if already_notified:
            return  # уже уведомляли, ждём, пока пользователь привяжет ордер к шагу
        db_instance.execute_query(
            f"UPDATE public.order_pipelines SET step_ready_notified_at = CURRENT_TIMESTAMP WHERE id = {pipeline_id};"
        )
        _send_notification(row, met)
        logging.info(f"🪜 [LadderStepWatcher]: Условие следующего шага выполнено (pipeline_id={pipeline_id}), уведомление отправлено.")
    else:
        if already_notified:
            db_instance.execute_query(
                f"UPDATE public.order_pipelines SET step_ready_notified_at = NULL WHERE id = {pipeline_id};"
            )


def _send_notification(row: dict, met: dict):
    telegram_id = row.get("telegram_id")
    if not telegram_id:
        return

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logging.warning("⚠️ [LadderStepWatcher]: TELEGRAM_TOKEN отсутствует -- уведомление о готовности шага не отправлено.")
        return

    text = (
        f"🪜 Пора шаг {int(row['current_step'])} плана по *{row['symbol']}* "
        f"({row['strategy_name']}, портфель {row['portfolio_name']}): условие выполнено -- "
        f"~{abs(met['suggested_qty']):.0f} шт по ${met['suggested_price']:,.2f}."
    )
    try:
        requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": int(telegram_id), "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as send_err:
        logging.error(f"⚠️ [LadderStepWatcher]: Не удалось отправить уведомление о готовности шага: {send_err}")
