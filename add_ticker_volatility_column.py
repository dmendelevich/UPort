#!/usr/bin/env python3
"""
Добавляет tickers.signal_daily_volatility_pct -- общий примитив волатильности,
см. Claude/BACKLOG.md (2026-07-30). Заполняется ночной синхронизацией
(site_connectors/sync_signals_yf.py), до первого прогона NULL -- потребители
(PriceMoveWatcher, протухание приказов/алертов) откатываются на плоский порог.
"""
import logging
from database import db_sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

STATEMENTS = [
    "ALTER TABLE public.tickers ADD COLUMN IF NOT EXISTS signal_daily_volatility_pct NUMERIC;",
    "COMMENT ON COLUMN public.tickers.signal_daily_volatility_pct IS "
    "'Std дневных %% изменений цены за DAILY_VOLATILITY_WINDOW_DAYS (settings.py), "
    "считается ночью в sync_signals_yf.py. Общий примитив для PriceMoveWatcher и "
    "протухания приказов/алертов, см. Claude/BACKLOG.md 2026-07-30.';",
]


def run():
    for stmt in STATEMENTS:
        logging.info(f"Выполняю: {stmt[:80]}...")
        db_sys.execute_query(stmt)
    logging.info("Готово.")


if __name__ == "__main__":
    run()
