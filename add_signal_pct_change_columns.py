#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Блок 2 "Поведение" body-стандарта карточки тикера (см. Claude/05_strategy_screen_and_kubiki.md,
# Claude/07_glossary.md): простое % изменение цены закрытия за 1д/1нед/1мес/1год. Считается в
# site_connectors/sync_signals_yf.py из того же годового ряда Yahoo, что и SMA/RSI/MACD.
ALTER_TABLE = """
    ALTER TABLE public.tickers
    ADD COLUMN IF NOT EXISTS signal_pct_1d NUMERIC,
    ADD COLUMN IF NOT EXISTS signal_pct_1w NUMERIC,
    ADD COLUMN IF NOT EXISTS signal_pct_1m NUMERIC,
    ADD COLUMN IF NOT EXISTS signal_pct_1y NUMERIC;
"""

COMMENTS = [
    "COMMENT ON COLUMN public.tickers.signal_pct_1d IS '% изменения цены закрытия за 1 торговый день (см. sync_signals_yf.py).';",
    "COMMENT ON COLUMN public.tickers.signal_pct_1w IS '% изменения цены закрытия за ~5 торговых дней (неделя).';",
    "COMMENT ON COLUMN public.tickers.signal_pct_1m IS '% изменения цены закрытия за ~21 торговый день (месяц).';",
    "COMMENT ON COLUMN public.tickers.signal_pct_1y IS '% изменения цены закрытия за весь скачанный годовой ряд (от самой старой доступной точки).';",
]


def run():
    logging.info("Добавляю колонки tickers.signal_pct_1d/1w/1m/1y (если ещё не существуют)...")
    db_sys.execute_query(ALTER_TABLE)
    for c in COMMENTS:
        db_sys.execute_query(c)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
