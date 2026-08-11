#!/usr/bin/env python3
"""
Тема «Защита капитала» (Claude/19_price_move_protection_design.md, сигнал B):
- assets.peak_price_since_entry -- бегущий максимум цены с момента покупки
  (нужен для трейлинг-стопа), инициализируется avg_price для уже держащихся
  позиций (честный старт -- реального пика ещё не отслеживали).
- strategies.rules_config / strategy_templates.rules_config -- K-множители
  волатильности: tactic_stop_loss_k (все три содержательные стратегии) и
  tactic_trailing_stop_k (только Револьверная/Консервативная -- Трендовой
  трейлинг не нужен, откалибровано бэктестом, см. дизайн-документ).
- Убирается tactic_trailing_stop_pct у Трендовой -- дремлющий, никогда не
  использовавшийся параметр, заменён явным решением "не нужен".
Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) assets.peak_price_since_entry ...")
db_sys.execute_query("""
    ALTER TABLE public.assets
    ADD COLUMN IF NOT EXISTS peak_price_since_entry NUMERIC;
""")
print("   готово.")

print("2) Бэкфилл peak_price_since_entry = avg_price для существующих позиций ...")
result = db_sys.execute_query("""
    UPDATE public.assets
    SET peak_price_since_entry = avg_price
    WHERE peak_price_since_entry IS NULL
    RETURNING id;
""")
print(f"   обновлено позиций: {len(result) if isinstance(result, list) else 0}")

print("3) tactic_stop_loss_k -- strategy_templates (заводские дефолты для будущих портфелей) ...")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || '{"tactic_stop_loss_k": 5.0}'::jsonb
    WHERE system_key = 'TREND_FOLLOWING'
    RETURNING system_key;
""")
print(f"   {result}")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || '{"tactic_stop_loss_k": 6.0}'::jsonb
    WHERE system_key = 'REVOLVER'
    RETURNING system_key;
""")
print(f"   {result}")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || '{"tactic_stop_loss_k": 5.0}'::jsonb
    WHERE system_key = 'CONSERVATIVE_ACCUMULATION'
    RETURNING system_key;
""")
print(f"   {result}")

print("4) tactic_trailing_stop_k -- strategy_templates (только Револьверная/Консервативная) ...")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || '{"tactic_trailing_stop_k": 11.0}'::jsonb
    WHERE system_key IN ('REVOLVER', 'CONSERVATIVE_ACCUMULATION')
    RETURNING system_key;
""")
print(f"   {result}")

print("5) Убираем дремлющий tactic_trailing_stop_pct у Трендовой (strategy_templates) ...")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config - 'tactic_trailing_stop_pct'
    WHERE system_key = 'TREND_FOLLOWING'
    RETURNING system_key;
""")
print(f"   {result}")

print("6) То же самое -- strategies (уже существующие портфели) ...")
result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || '{"tactic_stop_loss_k": 5.0}'::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'TREND_FOLLOWING'
    RETURNING s.id;
""")
print(f"   Трендовая tactic_stop_loss_k=5.0: {result}")

result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || '{"tactic_stop_loss_k": 6.0}'::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'REVOLVER'
    RETURNING s.id;
""")
print(f"   Револьверная tactic_stop_loss_k=6.0: {result}")

result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || '{"tactic_stop_loss_k": 5.0}'::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'CONSERVATIVE_ACCUMULATION'
    RETURNING s.id;
""")
print(f"   Консервативная tactic_stop_loss_k=5.0: {result}")

result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || '{"tactic_trailing_stop_k": 11.0}'::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key IN ('REVOLVER', 'CONSERVATIVE_ACCUMULATION')
    RETURNING s.id;
""")
print(f"   Р/К tactic_trailing_stop_k=11.0: {result}")

result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config - 'tactic_trailing_stop_pct'
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'TREND_FOLLOWING'
    RETURNING s.id;
""")
print(f"   Трендовая убран tactic_trailing_stop_pct: {result}")

print("\nГотово.")
