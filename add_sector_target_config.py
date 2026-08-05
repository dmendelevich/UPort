#!/usr/bin/env python3
"""
Тема «Секторальная диверсификация портфеля» (Claude/BACKLOG.md №82):
- portfolios.sector_target_config (jsonb) -- потолок доли каждого сектора от
  total_capital портфеля, срабатывание одностороннее (только превышение).
- Бэкфилл заводским значением (settings.DEFAULT_SECTOR_TARGET_CONFIG) для всех
  существующих портфелей, у которых поле ещё пустое.
Разовый скрипт, запускается один раз вручную.
"""
import json
import sys
sys.path.append('/root/UPort')

import settings
from database import db_sys

print("1) portfolios.sector_target_config ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS sector_target_config JSONB;
""")
print("   готово.")

print("2) Бэкфилл заводским вектором для существующих портфелей без значения ...")
default_json = json.dumps(settings.DEFAULT_SECTOR_TARGET_CONFIG, ensure_ascii=False, separators=(',', ':'))
result = db_sys.execute_query(
    """
    UPDATE public.portfolios
    SET sector_target_config = %s::jsonb
    WHERE sector_target_config IS NULL
    RETURNING id, name;
    """,
    (default_json,)
)
print(f"   обновлено портфелей: {result}")
