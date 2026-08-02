#!/usr/bin/env python3
"""
Добавляет tickers.signal_ema20_streak_days -- см. Claude/12_investment_goal_and_mechanisms_roadmap.md
и Claude/BACKLOG.md (2026-08-02). Заполняется ночной синхронизацией
(site_connectors/sync_signals_yf.py), до первого прогона NULL.
"""
import logging
from database import db_sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

STATEMENTS = [
    "ALTER TABLE public.tickers ADD COLUMN IF NOT EXISTS signal_ema20_streak_days INTEGER;",
    "COMMENT ON COLUMN public.tickers.signal_ema20_streak_days IS "
    "'Знаковая длина серии: сколько торговых дней подряд цена закрытия по одну сторону "
    "от EMA20 (+N выше / -N ниже), считается ночью в sync_signals_yf.py. Подтверждённый "
    "слом тренда = смена знака серии, устойчиво держащаяся >= TREND_REVERSAL_CONFIRM_DAYS "
    "(settings.py). См. Claude/07_glossary.md, Claude/BACKLOG.md 2026-08-02.';",
]


def run():
    for stmt in STATEMENTS:
        logging.info(f"Выполняю: {stmt[:80]}...")
        db_sys.execute_query(stmt)
    logging.info("Готово.")


if __name__ == "__main__":
    run()
