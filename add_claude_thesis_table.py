#!/usr/bin/env python3
"""
Тема «Мандат ПБумКлод» (Claude/BACKLOG.md, 2026-08-29) -- письменный тезис при
каждой покупке обязателен (согласовано с пользователем): без него между отдельными
пробуждениями агента не осталось бы записи, зачем держится конкретная позиция, а
выход из позиции ПБумКлод целиком на суждении агента, не на механическом бэкстопе.

Одна АКТИВНАЯ строка на держимую позицию (portfolio_id, listing_id), обновляется
при пересмотре взгляда (не плодит дубли); при продаже -- closed_at проставляется,
строка остаётся архивом (честная последующая проверка "совпала ли причина продажи
с причиной покупки"), не удаляется.

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) public.claude_position_thesis ...")
db_sys.execute_query("""
    CREATE TABLE IF NOT EXISTS public.claude_position_thesis (
        id SERIAL PRIMARY KEY,
        portfolio_id INTEGER NOT NULL REFERENCES public.portfolios(id) ON DELETE CASCADE,
        listing_id INTEGER NOT NULL REFERENCES public.listings(id),
        thesis TEXT NOT NULL,
        exit_criteria TEXT,
        created_at TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
        updated_at TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
        closed_at TIMESTAMP(0) WITHOUT TIME ZONE,
        close_reason TEXT
    );
""")
print("   готово.")

print("2) Уникальный частичный индекс -- максимум одна АКТИВНАЯ строка на позицию ...")
db_sys.execute_query("""
    CREATE UNIQUE INDEX IF NOT EXISTS claude_thesis_one_active_per_position
    ON public.claude_position_thesis (portfolio_id, listing_id)
    WHERE closed_at IS NULL;
""")
print("   готово.")

print("\nГотово.")
