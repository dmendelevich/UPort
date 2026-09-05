#!/usr/bin/env python3
"""
Тема «Полигон» (Claude/BACKLOG.md №170, 2026-09-05) -- живой (не бэктест)
испытательный стенд для находок тем #167/#168: экспериментальный жёсткий
SL/TP вместо реального трейлинга Револьверной, с предохранителем по VIX,
который останавливает НОВЫЕ покупки, если рынок выходит за пределы, реально
пройденные во всех проверках темы (VIX вне [13.5, 52.3]).

«ПБумПолигон» -- execution_mode='ADVISORY' (НЕ 'AUTO'!) -- не должен попадать
в run_auto_paper_cycle (Claude/BACKLOG.md №169, 15-минутный цикл ПБумАвто) --
тот применил бы РЕАЛЬНЫЙ трейлинг/confirm_days Револьверной поверх позиций
полигона, ровно то, чего нужно избежать. Содержательные стратегии заведены,
но пассивны (is_screening_active=false) -- используются только как источник
правил входа (analytics/polygon_paper_trader.py::screen_universe_for_strategy),
сами позиции лягут в «Неопределённая», тем же путём, что и «Купить вне
стратегии»/ПБумКлод -- реальный трейлинг-стоп/подтверждение слома тренда
физически её не касаются.

owner_id=1 (dmend), broker_id=NULL, $20 000 стартового капитала (не $50 000,
как у остальных бумажных -- сознательно меньше, тестовый полигон).

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) portfolios -- добавляю колонки предохранителя (auto_trading_paused) ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS auto_trading_paused BOOLEAN NOT NULL DEFAULT FALSE;
""")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS auto_trading_paused_at TIMESTAMP(0);
""")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS auto_trading_paused_reason TEXT;
""")
print("   готово.")


def create_paper_portfolio(name: str, execution_mode: str, starting_cash: float, activate_content_strategies: bool):
    existing = db_sys.execute_query("SELECT id FROM public.portfolios WHERE name = %s;", (name,))
    if existing:
        print(f"   «{name}» уже существует (id={existing[0]['id']}), пропускаю.")
        return

    result = db_sys.execute_query("""
        WITH new_portfolio AS (
            INSERT INTO public.portfolios (name, owner_id, broker_id, execution_mode)
            VALUES (%s, 1, NULL, %s)
            RETURNING id
        ),
        all_strategies AS (
            INSERT INTO public.strategies
                (portfolio_id, template_id, strategy_name, rules_config, human_philosophy,
                 strategy_share_pct, is_active, is_screening_active)
            SELECT
                new_portfolio.id, st.id, st.template_name, st.rules_config, st.human_philosophy,
                CASE
                    WHEN st.system_key = 'UNALLOCATED' THEN 0.00
                    WHEN st.system_key IN ('REVOLVER', 'CONSERVATIVE_ACCUMULATION', 'TREND_FOLLOWING') AND NOT %s THEN 0.00
                    ELSE st.recommended_share_pct
                END,
                true,
                CASE
                    WHEN st.system_key = 'UNALLOCATED' THEN false
                    WHEN st.system_key IN ('REVOLVER', 'CONSERVATIVE_ACCUMULATION', 'TREND_FOLLOWING') THEN %s
                    ELSE true
                END
            FROM new_portfolio, public.strategy_templates st
            RETURNING id
        ),
        new_account AS (
            INSERT INTO public.accounts
                (user_id, portfolio_id, broker_id, account_number, account_type, currency_id,
                 cash_available, cash_reserved, assets_value)
            SELECT 1, new_portfolio.id, NULL, 'PAPER-' || new_portfolio.id, 'trade', 'USD',
                   %s, 0, 0
            FROM new_portfolio
            RETURNING account_number
        )
        SELECT new_portfolio.id AS portfolio_id, new_account.account_number
        FROM new_portfolio, new_account;
    """, (name, execution_mode, activate_content_strategies, activate_content_strategies, starting_cash))
    print(f"   создан «{name}»: {result}")


print("2) «ПБумПолигон» (ADVISORY, $20 000, содержательные стратегии пассивны) ...")
create_paper_portfolio("ПБумПолигон", "ADVISORY", 20000.00, activate_content_strategies=False)

print("\nГотово.")
