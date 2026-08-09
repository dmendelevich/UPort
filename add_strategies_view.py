#!/usr/bin/env python3
"""
Стандартизация порядка отображения стратегий (Claude/BACKLOG.md, обсуждение 2026-08-XX):
- strategy_templates.display_order (int) -- заводской порядок отображения стратегий
  во всех списках/наборах кнопок бота, с запасом (10/20/30...) под будущую вставку.
- v_strategies_full -- новый view слоя 0 (по образцу v_listings_tickers, см.
  Claude/02_universal_views.md), одна строка = одна стратегия (без fan-out по
  активам, в отличие от v_strategy_assets_full) -- убирает дублирование джойна
  strategies JOIN strategy_templates, повторявшегося вручную в 4-5 местах кода.
Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) strategy_templates.display_order ...")
db_sys.execute_query("""
    ALTER TABLE public.strategy_templates
    ADD COLUMN IF NOT EXISTS display_order INTEGER;
""")

DISPLAY_ORDER = {
    "INDEX_CORE": 10,
    "CONSERVATIVE_ACCUMULATION": 20,
    "TREND_FOLLOWING": 30,
    "REVOLVER": 40,
    "UNALLOCATED": 50,
    "CASH_RESERVE": 60,
}
for system_key, order in DISPLAY_ORDER.items():
    db_sys.execute_query(
        "UPDATE public.strategy_templates SET display_order = %s WHERE system_key = %s;",
        (order, system_key)
    )

db_sys.execute_query("""
    ALTER TABLE public.strategy_templates
    ALTER COLUMN display_order SET NOT NULL;
""")
print("   готово.")

print("2) v_strategies_full ...")
db_sys.execute_query("DROP VIEW IF EXISTS public.v_strategies_full CASCADE;")
db_sys.execute_query("""
    CREATE VIEW public.v_strategies_full AS
    SELECT
        s.id AS strategy_id,
        s.portfolio_id,
        s.strategy_name,
        s.strategy_share_pct,
        s.rules_config,
        s.human_philosophy,
        s.is_active,
        s.is_screening_active,
        s.template_id,
        st.system_key,
        st.template_name,
        st.display_order
    FROM public.strategies s
    JOIN public.strategy_templates st ON s.template_id = st.id;
""")
print("   готово.")

print("3) Проверка ...")
rows = db_sys.execute_query("""
    SELECT strategy_id, strategy_name, system_key, display_order
    FROM public.v_strategies_full
    WHERE portfolio_id = 1
    ORDER BY display_order;
""")
for r in rows:
    print(f"   {r['display_order']:>3} {r['system_key']:<28} {r['strategy_name']}")
