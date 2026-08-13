#!/usr/bin/env python3
"""
Найдено 2026-08-13 при разборе переноса Револьверная->Трендовая в «ПБум»: strategy_tactics
(шаги лесенки входа) заполнялась только вручную, по одному разу для каждой комбинации
портфель+стратегия (П10 Револьверная, П136 Консервативная+Трендовая) -- не была частью
бутстрапа нового портфеля (bot_handlers/portfolio_admin.py::process_execute). Из-за этого
у «ПБум» (и у П10 Консервативной/Трендовой, П136 Револьверной, ПМ -- всех content-стратегий,
кроме тех трёх исходных) вообще нет строк strategy_tactics -- LadderStepWatcher для них
молчит не по замыслу, а по нехватке данных.

Тактика -- свойство ТИПА стратегии (system_key), не конкретного портфеля (число шагов и
их условия одинаковы для всех). Правильное место -- уровень шаблона, как уже сделано для
rules_config/human_philosophy. Этот скрипт:
1) заводит новую таблицу strategy_template_tactics (шаблонный аналог strategy_tactics);
2) сеет её тремя известными по факту рабочими наборами (Револьверная/Трендовая -- один
   шаг, рынок, немедленно; Консервативная -- три шага 30/30/40%, уже проверенные бэктестом
   и живой лесенкой П136);
3) бэкфиллит strategy_tactics для ВСЕХ существующих content-стратегий, у которых сегодня
   вообще нет ни одной строки (не только «ПБум» -- реюз механизма, не разовая заплатка).

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) Таблица strategy_template_tactics ...")
db_sys.execute_query("""
    CREATE TABLE IF NOT EXISTS public.strategy_template_tactics (
        id SERIAL PRIMARY KEY,
        template_id INTEGER NOT NULL REFERENCES public.strategy_templates(id),
        step_number INTEGER NOT NULL,
        budget_share_pct NUMERIC NOT NULL,
        trigger_conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
        UNIQUE (template_id, step_number)
    );
""")
print("   готово.")

print("2) Сев канонических шагов (по образцу уже проверенных живых П10/П136) ...")
seed_rows = [
    # (system_key, step_number, budget_share_pct, trigger_conditions)
    ("REVOLVER", 1, 100.0, {"mode": "market", "entry_trigger": "immediate"}),
    ("TREND_FOLLOWING", 1, 100.0, {"mode": "market", "entry_trigger": "immediate"}),
    ("CONSERVATIVE_ACCUMULATION", 1, 30.0, {"mode": "market", "entry_trigger": "immediate"}),
    ("CONSERVATIVE_ACCUMULATION", 2, 30.0, {"mode": "limit", "max_rsi": 40.0, "volatility_multiplier": 4.0, "require_confirmed_reversal": True}),
    ("CONSERVATIVE_ACCUMULATION", 3, 40.0, {"mode": "limit", "max_rsi": 30.0, "volatility_multiplier": 8.0, "require_confirmed_reversal": True}),
]
import json
for system_key, step_number, budget_share_pct, trigger_conditions in seed_rows:
    result = db_sys.execute_query("""
        INSERT INTO public.strategy_template_tactics (template_id, step_number, budget_share_pct, trigger_conditions)
        SELECT id, %s, %s, %s::jsonb FROM public.strategy_templates WHERE system_key = %s
        ON CONFLICT (template_id, step_number) DO NOTHING
        RETURNING id;
    """, (step_number, budget_share_pct, json.dumps(trigger_conditions), system_key))
    print(f"   {system_key} шаг {step_number}: {result}")

print("3) Бэкфилл strategy_tactics -- для КАЖДОЙ content-стратегии без единой строки ...")
result = db_sys.execute_query("""
    INSERT INTO public.strategy_tactics (strategy_id, step_number, budget_share_pct, trigger_conditions)
    SELECT s.id, tt.step_number, tt.budget_share_pct, tt.trigger_conditions
    FROM public.strategies s
    JOIN public.strategy_template_tactics tt ON tt.template_id = s.template_id
    WHERE NOT EXISTS (
        SELECT 1 FROM public.strategy_tactics existing WHERE existing.strategy_id = s.id
    )
    RETURNING strategy_id;
""")
print(f"   добавлено строк: {len(result) if isinstance(result, list) else 0}")

print("\nГотово.")
