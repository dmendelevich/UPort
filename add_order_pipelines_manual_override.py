#!/usr/bin/env python3
"""
BACKLOG.md №73 -- "Всё равно купить/продать" для бумажного портфеля: нужно
отличать сделки по совету системы от ручного форс-оверрайда пользователя,
чтобы не путать их через год при измерении эффективности. Разовый скрипт.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("order_pipelines.is_manual_override ...")
db_sys.execute_query("""
    ALTER TABLE public.order_pipelines
    ADD COLUMN IF NOT EXISTS is_manual_override BOOLEAN NOT NULL DEFAULT FALSE;
""")
print("готово.")
