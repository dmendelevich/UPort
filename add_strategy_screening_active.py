#!/usr/bin/env python3
"""
Реализует итог обсуждения 2026-07-31 (Claude/BACKLOG.md, п.9, Трек B):
1. strategies.is_screening_active -- отдельно от существующего is_active (тот
   означает "жива ли строка/считать в капитале", этот -- "участвует ли в подборе
   новых кандидатов" (CashDeploymentAdvisor/проверка совместимости). НЕ гасит
   слежение за выходом уже держимых позиций и не выключает саму строку из общего
   капитала -- только подбор новых.
2. strategy_templates.recommended_share_pct -- заводская доля: для содержательных
   стратегий (REVOLVER/CONSERVATIVE_ACCUMULATION/TREND_FOLLOWING) это "доля, ЕСЛИ/
   КОГДА стратегию включат" (не применяется автоматически); для CASH_RESERVE/
   UNALLOCATED -- реальная доля новых портфелей с первого дня.
3. Универсальный набор шаблонов: каждый портфель должен иметь ВСЕ содержательные
   стратегии (не только те, что уже используются) -- новые заводятся Пассивными
   с долей 0%, как готовая цель будущего переноса и место для включения, когда
   понадобится. Бэкафилл для уже существующих портфелей (П10/П136/ПМ).
"""
import logging
from database import db_sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

SCHEMA_STATEMENTS = [
    # is_screening_active по умолчанию true -- существующие содержательные
    # стратегии уже реально используются, не должны стать "пассивными" молча.
    """
    ALTER TABLE public.strategies
        ADD COLUMN IF NOT EXISTS is_screening_active BOOLEAN NOT NULL DEFAULT true;
    """,
    """
    COMMENT ON COLUMN public.strategies.is_screening_active IS
        'Участвует ли стратегия в подборе НОВЫХ кандидатов (CashDeploymentAdvisor, '
        'проверка совместимости) -- НЕ то же самое, что is_active (жива ли строка / '
        'считать в общем капитале). Пассивная стратегия по-прежнему считается в '
        'капитале и получает слежение за выходом уже держимых позиций, см. '
        'Claude/BACKLOG.md п.9, 2026-07-31.';
    """,
    "ALTER TABLE public.strategy_templates ADD COLUMN IF NOT EXISTS recommended_share_pct NUMERIC;",
    """
    COMMENT ON COLUMN public.strategy_templates.recommended_share_pct IS
        'Для содержательных стратегий -- рекомендуемая доля, ЕСЛИ/КОГДА стратегию '
        'включат (не применяется автоматически при создании портфеля, там 0%). '
        'Для CASH_RESERVE/UNALLOCATED -- реальная доля новых портфелей с первого дня.';
    """,
]

TEMPLATE_SHARE_UPDATES = [
    ("REVOLVER", 15.0),
    ("CONSERVATIVE_ACCUMULATION", 50.0),
    ("TREND_FOLLOWING", 25.0),
    ("CASH_RESERVE", 10.0),
    ("UNALLOCATED", 90.0),
]

CONTENT_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING")


def run():
    for stmt in SCHEMA_STATEMENTS:
        logging.info(f"Выполняю: {stmt.strip()[:80]}...")
        db_sys.execute_query(stmt)

    for system_key, pct in TEMPLATE_SHARE_UPDATES:
        db_sys.execute_query(f"""
            UPDATE public.strategy_templates SET recommended_share_pct = {pct}
            WHERE system_key = '{system_key}';
        """)
    logging.info("strategy_templates.recommended_share_pct проставлен.")

    # Бэкафилл: у каждого реального портфеля должны быть ВСЕ три содержательные
    # стратегии -- недостающие заводим Пассивными, доля 0%, rules_config/
    # human_philosophy копируем из шаблона (тот же приём, что и при создании
    # портфеля, portfolio_admin.py).
    portfolios = db_sys.execute_query("SELECT id, name FROM public.portfolios ORDER BY id;")
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    for p in portfolios:
        p_id = int(p["id"])
        existing = db_sys.execute_query(f"""
            SELECT st.system_key FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = {p_id};
        """)
        existing = existing if isinstance(existing, list) else ([existing] if existing else [])
        existing_keys = {r["system_key"] for r in existing if r}

        for key in CONTENT_SYSTEM_KEYS:
            if key in existing_keys:
                continue
            logging.info(f"  • Портфель {p_id} ({p['name']}): добавляю пассивную {key} (0%)...")
            db_sys.execute_query(f"""
                INSERT INTO public.strategies
                    (portfolio_id, template_id, strategy_name, rules_config, human_philosophy,
                     strategy_share_pct, is_active, is_screening_active)
                SELECT {p_id}, st.id, st.template_name, st.rules_config, st.human_philosophy,
                       0.00, true, false
                FROM public.strategy_templates st
                WHERE st.system_key = '{key}';
            """)

    logging.info("Готово.")


if __name__ == "__main__":
    run()
