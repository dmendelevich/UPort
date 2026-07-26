#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# PriceMoveWatcher (см. Claude/05_strategy_screen_and_kubiki.md, Claude/ARCHITECTURE/Time.md):
# короткая по времени история котировок одного листинга -- ручной кольцевой буфер
# (Postgres не имеет для этого встроенного механизма), см. analytics/quote_snapshot_utils.py.
# Стандарт времени UPort: TIMESTAMP(0) WITHOUT TIME ZONE, UTC.
CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS public.quote_snapshots (
        id SERIAL PRIMARY KEY,
        listing_id INT NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
        price NUMERIC NOT NULL,
        recorded_at TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
    );
"""

CREATE_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_quote_snapshots_listing_time
    ON public.quote_snapshots (listing_id, recorded_at DESC);
"""

COMMENT = """
    COMMENT ON TABLE public.quote_snapshots IS
        'Короткий кольцевой буфер последних котировок листинга (PriceMoveWatcher) -- НЕ полная история цен, размер ограничен PRICE_MOVE_WATCHER_BUFFER_SIZE в settings.py, старые строки удаляются вручную при каждой записи.';
"""


def run():
    logging.info("Создаю таблицу public.quote_snapshots (если ещё не существует)...")
    db_sys.execute_query(CREATE_TABLE)
    db_sys.execute_query(CREATE_INDEX)
    db_sys.execute_query(COMMENT)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
