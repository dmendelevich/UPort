#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Отметка "уже уведомили, что условие следующего шага лесенки выполнено" -- проверяется на
# цикле котировок (рыночно-зависимо), не на дайджесте (см. обсуждение 2026-07-24,
# Claude/09_pipeline_reconciliation.md). NULL -- условие не выполнено или ещё не проверено.
ALTER_TABLE = """
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS step_ready_notified_at TIMESTAMP WITHOUT TIME ZONE;
"""

COMMENT = """
    COMMENT ON COLUMN public.order_pipelines.step_ready_notified_at IS
        'Момент разового уведомления о том, что trigger_conditions следующего шага выполнено. NULL -- не выполнено или уже отработано.';
"""


def run():
    logging.info("Добавляю колонку order_pipelines.step_ready_notified_at (если ещё не существует)...")
    db_sys.execute_query(ALTER_TABLE)
    db_sys.execute_query(COMMENT)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
