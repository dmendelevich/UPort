#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Отметка "уже уведомили о протухании этого ордера" -- см. Claude/09_pipeline_reconciliation.md.
# NULL -- ещё не протух (или протух и вернулся в норму, снова готов к уведомлению).
ALTER_TABLE = """
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS stale_notified_at TIMESTAMP WITHOUT TIME ZONE;
"""

COMMENT = """
    COMMENT ON COLUMN public.order_pipelines.stale_notified_at IS
        'Момент разового уведомления о протухании pending_broker_order_id (цена ушла >5% от цены ордера). NULL -- не протух или вернулся в норму.';
"""


def run():
    logging.info("Добавляю колонку order_pipelines.stale_notified_at (если ещё не существует)...")
    db_sys.execute_query(ALTER_TABLE)
    db_sys.execute_query(COMMENT)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
