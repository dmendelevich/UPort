#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Персистентная (переживающая рестарт процесса) память "умных будильников" cron_scheduler.py:
# защита от повторного срабатывания в один и тот же UTC-день + честный статус выполнения
# (SUCCESS/FAILED по факту исключения, а не всегда "успех" в логе).
CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS public.cron_job_runs (
        job_name TEXT PRIMARY KEY,
        last_started_at TIMESTAMP(0) WITHOUT TIME ZONE,
        last_finished_at TIMESTAMP(0) WITHOUT TIME ZONE,
        last_status TEXT,
        last_error_message TEXT
    );
"""

COMMENT = """
    COMMENT ON TABLE public.cron_job_runs IS
        'Трек-запись последнего запуска каждого суточного/недельного будильника cron_scheduler.py -- защита от дублей при рестарте процесса (Restart=always) и честный статус SUCCESS/FAILED.';
"""


def run():
    logging.info("Создаю таблицу public.cron_job_runs (если ещё не существует)...")
    db_sys.execute_query(CREATE_TABLE)
    db_sys.execute_query(COMMENT)
    logging.info("✅ Готово.")


if __name__ == "__main__":
    run()
