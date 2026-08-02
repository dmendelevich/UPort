#!/usr/bin/env python3
"""
Добавляет require_confirmed_reversal=true в trigger_conditions шагов 2/3
Консервативного накопления (см. Claude/12_investment_goal_and_mechanisms_roadmap.md,
Claude/BACKLOG.md 2026-08-02). До сих пор шаги 2/3 триггерились только по RSI+просадке
(MU-кейс: докупка в падающий нож без подтверждения, что падение остановилось) --
LadderStepWatcher теперь дополнительно требует tickers.signal_ema20_streak_days
>= settings.TREND_REVERSAL_CONFIRM_DAYS, если ключ выставлен.
"""
import logging
from database import db_sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

STATEMENTS = [
    """
    UPDATE public.strategy_tactics
    SET trigger_conditions = trigger_conditions || '{"require_confirmed_reversal": true}'::jsonb
    WHERE strategy_id IN (
        SELECT s.id FROM public.strategies s
        JOIN public.strategy_templates tpl ON s.template_id = tpl.id
        WHERE tpl.system_key = 'CONSERVATIVE_ACCUMULATION'
    )
    AND step_number IN (2, 3);
    """,
]


def run():
    for stmt in STATEMENTS:
        logging.info(f"Выполняю: {stmt.strip()[:100]}...")
        db_sys.execute_query(stmt)
    logging.info("Готово.")


if __name__ == "__main__":
    run()
