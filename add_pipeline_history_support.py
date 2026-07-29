#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Разрешаем накопление истории циклов order_pipelines -- COMPLETED-строки больше не должны
# блокировать повторное рождение той же бумаги в той же стратегии (см.
# Claude/11_asset_lifecycle_and_plan.md, аудит БД 2026-07-29). Жёсткий уникальный индекс
# заменяется частичным: уникальность требуется только среди ещё не завершённых планов.
DROP_OLD_INDEX = """
    ALTER TABLE public.order_pipelines DROP CONSTRAINT IF EXISTS order_pipelines_unique_run;
"""

CREATE_PARTIAL_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS order_pipelines_unique_active_run
    ON public.order_pipelines (portfolio_id, listing_id, strategy_id)
    WHERE pipeline_status IN ('PENDING', 'ACTIVE');
"""

COMMENT_INDEX = """
    COMMENT ON INDEX public.order_pipelines_unique_active_run IS
        'Уникальность только среди PENDING/ACTIVE планов -- COMPLETED-строки хранятся как история циклов, не блокируют повторное рождение той же бумаги в той же стратегии.';
"""

# Отметка "уже уведомили о наступлении календарного чек-пойнта плана" -- тот же паттерн,
# что и у stale_notified_at/step_ready_notified_at. NULL -- ещё не уведомляли или чек-пойнт снят.
ADD_CHECKPOINT_COLUMN = """
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS checkpoint_notified_at TIMESTAMP WITHOUT TIME ZONE;
"""

COMMENT_CHECKPOINT = """
    COMMENT ON COLUMN public.order_pipelines.checkpoint_notified_at IS
        'Момент последнего уведомления о календарном чек-пойнте плана (см. Claude/11_asset_lifecycle_and_plan.md). NULL -- ещё не уведомляли или чек-пойнт снят.';
"""


def run():
    logging.info("Заменяю жёсткий уникальный индекс order_pipelines на частичный (только PENDING/ACTIVE)...")
    db_sys.execute_query(DROP_OLD_INDEX)
    db_sys.execute_query(CREATE_PARTIAL_INDEX)
    db_sys.execute_query(COMMENT_INDEX)

    logging.info("Добавляю колонку order_pipelines.checkpoint_notified_at (если ещё не существует)...")
    db_sys.execute_query(ADD_CHECKPOINT_COLUMN)
    db_sys.execute_query(COMMENT_CHECKPOINT)

    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
