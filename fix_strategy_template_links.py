#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import db_sys

# Шаг 1: система смысловых ключей у словаря "заводских настроек" strategy_templates.
# Уникальный, обязательный для всех строк (и структурных, и содержательных).
STEP_1_SYSTEM_KEYS = [
    "ALTER TABLE public.strategy_templates ADD COLUMN system_key VARCHAR(30);",
    "UPDATE public.strategy_templates SET system_key = 'REVOLVER' WHERE template_name = 'Револьверная стратегия';",
    "UPDATE public.strategy_templates SET system_key = 'CONSERVATIVE_ACCUMULATION' WHERE template_name = 'Консервативное накопление';",
    "UPDATE public.strategy_templates SET system_key = 'TREND_FOLLOWING' WHERE template_name = 'Стратегия следования за трендом';",
    "UPDATE public.strategy_templates SET system_key = 'CASH_RESERVE' WHERE template_name = 'Кэш/Резерв';",
    "UPDATE public.strategy_templates SET system_key = 'UNALLOCATED' WHERE template_name = 'Неопределенная стратегия';",
    "ALTER TABLE public.strategy_templates ALTER COLUMN system_key SET NOT NULL;",
    "ALTER TABLE public.strategy_templates ADD CONSTRAINT strategy_templates_system_key_unique UNIQUE (system_key);",
]

# Шаг 2: связь strategies -> strategy_templates (пока nullable, до бэкафилла).
STEP_2_ADD_TEMPLATE_ID = [
    "ALTER TABLE public.strategies ADD COLUMN template_id INTEGER REFERENCES public.strategy_templates(id);",
]

# Шаг 3: бэкафилл существующих строк по совпадению имени (разовая операция,
# больше нигде в системе имя стратегии для связи не используется).
STEP_3_BACKFILL = [
    """
    UPDATE public.strategies s
    SET template_id = st.id
    FROM public.strategy_templates st
    WHERE s.strategy_name = st.template_name
      AND s.template_id IS NULL;
    """,
]

# Шаг 4: закрываем дыру -- у портфеля 1 нет буферной стратегии "Неопределенная".
STEP_4_FILL_GAP = [
    """
    INSERT INTO public.strategies (portfolio_id, template_id, strategy_name, rules_config, human_philosophy, strategy_share_pct, is_active)
    SELECT 1, id, template_name, rules_config, human_philosophy, 0.00, true
    FROM public.strategy_templates
    WHERE system_key = 'UNALLOCATED'
      AND NOT EXISTS (
          SELECT 1 FROM public.strategies WHERE portfolio_id = 1 AND template_id = public.strategy_templates.id
      );
    """,
]

# Шаг 5: теперь у всех строк должен быть template_id -> делаем обязательным + индекс
# под новый паттерн запроса (portfolio_id, template_id).
STEP_5_ENFORCE_NOT_NULL = [
    "ALTER TABLE public.strategies ALTER COLUMN template_id SET NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_strategies_portfolio_template ON public.strategies(portfolio_id, template_id);",
]

STEPS_BEFORE_NOT_NULL = [
    ("Шаг 1: system_key в strategy_templates", STEP_1_SYSTEM_KEYS),
    ("Шаг 2: template_id в strategies", STEP_2_ADD_TEMPLATE_ID),
    ("Шаг 3: бэкафилл template_id", STEP_3_BACKFILL),
    ("Шаг 4: недостающая буферная стратегия портфеля 1", STEP_4_FILL_GAP),
]


def run():
    for label, statements in STEPS_BEFORE_NOT_NULL:
        logging.info(f"--- {label} ---")
        for stmt in statements:
            res = db_sys.execute_query(stmt)
            if not res or res[0].get("status") != "success":
                logging.error(f"Сбой при выполнении: {stmt.strip()[:100]}...")
                return False

    logging.info("Проверка перед наложением NOT NULL: все строки strategies должны иметь template_id...")
    check = db_sys.execute_query("SELECT COUNT(*) AS cnt FROM public.strategies WHERE template_id IS NULL;")
    remaining = int(check[0]["cnt"]) if check else -1
    if remaining != 0:
        logging.error(f"ОСТАНОВКА: осталось {remaining} строк без template_id, NOT NULL не накладываю!")
        return False

    logging.info("--- Шаг 5: NOT NULL + индекс ---")
    for stmt in STEP_5_ENFORCE_NOT_NULL:
        res = db_sys.execute_query(stmt)
        if not res or res[0].get("status") != "success":
            logging.error(f"Сбой при выполнении: {stmt.strip()[:100]}...")
            return False

    logging.info("Готово.")
    return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
