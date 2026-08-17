#!/usr/bin/env python3
"""
Тема «К сделке» (Claude/BACKLOG.md №122/123) -- order_pipelines.entry_trigger_override
(jsonb, NULL по умолчанию): хранит выбор пользователя между двумя шпаргалками (ASAP/
оптимальная цена) для ШАГА 1 конкретного Плана -- strategy_tactics задаёт ОДНО общее
условие на всю стратегию (сегодня всегда mode:market), а шпаргалка выбирается
индивидуально при каждом решении купить/продать. NULL -- используется общий дефолт
стратегии (старое поведение, ничего не меняется для уже существующих Планов).
Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("order_pipelines.entry_trigger_override ...")
db_sys.execute_query("""
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS entry_trigger_override JSONB;
""")
print("готово.")
