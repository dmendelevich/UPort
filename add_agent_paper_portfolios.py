#!/usr/bin/env python3
"""
Тема «Агентские бумажные портфели» (Claude/BACKLOG.md, 2026-08-29) -- два новых
бумажных портфеля по образцу «ПБум» (add_paper_portfolio_infra.py), не трогая
сам ПБум (пользователь тестирует на нём интерфейс, остаётся как есть):

- «ПБумАвто» -- execution_mode='AUTO' (новое значение), по существующим
  стратегиям (Р/К/Т с заводскими recommended_share_pct, как у ПБум) --
  исполняется полностью автоматически новым analytics/auto_paper_trader.py,
  без подтверждения человеком.
- «ПБумКлод» -- execution_mode='ADVISORY' (не 'AUTO' -- НЕ должен попадать в
  автоматический цикл CashDeploymentAdvisor/PositionExitEvaluator, решения
  принимаются отдельно, вручную, при будущем пробуждении агента). Содержательные
  стратегии (Р/К/Т) заведены, но НЕ активны (is_screening_active=false,
  strategy_share_pct=0) -- весь мандат ещё не согласован (см. Claude/BACKLOG.md),
  свободные покупки лягут в «Неопределённая» (тот же путь, что «Купить вне
  стратегии», bot_handlers/ticker_search.py) до отдельного решения.

Оба -- owner_id=1 (dmend), broker_id=NULL, $50 000 стартового капитала, США.

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) portfolios.execution_mode -- расширяю CHECK на 'AUTO' ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios DROP CONSTRAINT IF EXISTS portfolios_execution_mode_check;
""")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD CONSTRAINT portfolios_execution_mode_check
    CHECK (execution_mode IN ('ADVISORY', 'CONFIRM', 'AUTO'));
""")
print("   готово.")


def create_paper_portfolio(name: str, execution_mode: str, activate_content_strategies: bool):
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
                   50000.00, 0, 0
            FROM new_portfolio
            RETURNING account_number
        )
        SELECT new_portfolio.id AS portfolio_id, new_account.account_number
        FROM new_portfolio, new_account;
    """, (name, execution_mode, activate_content_strategies, activate_content_strategies))
    print(f"   создан «{name}»: {result}")


print("2) «ПБумАвто» (AUTO, по существующим стратегиям) ...")
create_paper_portfolio("ПБумАвто", "AUTO", activate_content_strategies=True)

print("3) «ПБумКлод» (ADVISORY, мандат не согласован -- содержательные стратегии пассивны) ...")
create_paper_portfolio("ПБумКлод", "ADVISORY", activate_content_strategies=False)

print("\nГотово.")
