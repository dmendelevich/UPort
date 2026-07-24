#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Связь конкретного шага order_pipelines с конкретным ордером брокера, ожидаемым на этом
# шаге -- заводится вручную пользователем через бота (см. Claude/09_pipeline_reconciliation.md).
# Очищается, когда шаг засчитан сверкой.
ALTER_TABLE = """
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS pending_broker_order_id VARCHAR;
"""

COMMENT = """
    COMMENT ON COLUMN public.order_pipelines.pending_broker_order_id IS
        'ID приказа брокера (orders.broker_order_id), ожидаемого на current_step -- проставляется вручную через бота, очищается при засчитанном шаге.';
"""


def run():
    logging.info("Добавляю колонку order_pipelines.pending_broker_order_id (если ещё не существует)...")
    db_sys.execute_query(ALTER_TABLE)
    db_sys.execute_query(COMMENT)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
