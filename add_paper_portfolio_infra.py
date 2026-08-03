#!/usr/bin/env python3
"""
Фаза 1 темы «Бумажный портфель» (см. Claude/14_paper_portfolio.md):
- portfolios.execution_mode (ADVISORY | CONFIRM)
- portfolio_value_history (снимок NAV, переиспользуем позже и для реальных портфелей)
- сам портфель «ПБум» + 5 стратегий + виртуальный счёт на $50 000
Разовый скрипт, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) portfolios.execution_mode ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(20) NOT NULL DEFAULT 'ADVISORY'
    CHECK (execution_mode IN ('ADVISORY', 'CONFIRM'));
""")
print("   готово.")

print("2) portfolio_value_history ...")
db_sys.execute_query("""
    CREATE TABLE IF NOT EXISTS public.portfolio_value_history (
        id SERIAL PRIMARY KEY,
        portfolio_id INTEGER NOT NULL REFERENCES public.portfolios(id) ON DELETE CASCADE,
        snapshot_date DATE NOT NULL,
        total_value NUMERIC(18, 2) NOT NULL,
        created_at TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
        UNIQUE (portfolio_id, snapshot_date)
    );
""")
print("   готово.")

print("3) Портфель «ПБум» + 5 стратегий + виртуальный счёт ...")
existing = db_sys.execute_query("SELECT id FROM public.portfolios WHERE name = 'ПБум';")
if existing:
    print(f"   уже существует (id={existing[0]['id']}), пропускаю создание.")
else:
    result = db_sys.execute_query("""
        WITH new_portfolio AS (
            INSERT INTO public.portfolios (name, owner_id, broker_id, execution_mode)
            VALUES ('ПБум', 1, NULL, 'CONFIRM')
            RETURNING id
        ),
        all_strategies AS (
            INSERT INTO public.strategies
                (portfolio_id, template_id, strategy_name, rules_config, human_philosophy,
                 strategy_share_pct, is_active, is_screening_active)
            SELECT
                new_portfolio.id, st.id, st.template_name, st.rules_config, st.human_philosophy,
                CASE WHEN st.system_key = 'UNALLOCATED' THEN 0.00 ELSE st.recommended_share_pct END,
                true,
                CASE WHEN st.system_key = 'UNALLOCATED' THEN false ELSE true END
            FROM new_portfolio, public.strategy_templates st
            RETURNING id
        ),
        new_account AS (
            INSERT INTO public.accounts
                (user_id, portfolio_id, broker_id, account_number, account_type, currency_id,
                 cash_available, cash_reserved, assets_value)
            SELECT 1, new_portfolio.id, NULL, 'PAPER-' || new_portfolio.id, 'trade', 'USD',
                   50000.00, 0, 0
            FROM new_portfolio
            RETURNING account_number
        )
        SELECT new_portfolio.id AS portfolio_id, new_account.account_number
        FROM new_portfolio, new_account;
    """)
    print(f"   создан: {result}")

print("Готово.")
