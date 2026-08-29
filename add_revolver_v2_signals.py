#!/usr/bin/env python3
"""
Тема «Эффективность Револьверной, часть 2» (Claude/BACKLOG.md, 2026-08-29) --
реализация выводов бэктеста (ad hoc, yfinance, вне кода UPort):
  1) фильтр роста (revenue_growth) + отсутствие/низкие дивиденды на входе --
     3x разница в среднем результате сделки между growth/no-div и dividend/value
     когортами на одинаковых правилах входа-выхода;
  2) снижение tactic_target_profit_pct 15.0 -> 6.0 -- медиана результата сделки
     на 15% отрицательна (редкий крупный куш, чаще проигрыш), на 5-6% -- устойчиво
     положительна, выше win-rate;
  3) сужение tactic_trailing_stop_k 11.0 -> 5.0 (НЕ tactic_stop_loss_k -- тот
     остаётся 6.0, сужение стоп-лосса резко ухудшает результат, проверено сеткой);
  4) два новых индикатора -- объём дня/20-дневное среднее и глубина просадки от
     20-дневного максимума (нормированная на волатильность) -- оба дали реальный,
     количественно подтверждённый сигнал в бэктесте.

Схема:
- tickers.signal_volume_ratio_20d (numeric) -- объём дня / средний объём за 20
  торговых дней (>1.0 -- выше среднего, признак настоящей капитуляции/интереса,
  не тихого дрейфа).
- tickers.signal_price_to_20d_high_pct (numeric) -- % положения цены относительно
  20-дневного максимума (отрицательно ниже пика) -- используется как РАНЖИРОВАНИЕ
  (не гейт): _score_revolver нормирует на signal_daily_volatility_pct и инвертирует
  знак (глубже просадка = больше ranking_value = выше в списке кандидатов),
  заменяет собой upside_pct (аналитический таргет) -- тот нигде за пределами
  _score_revolver не использовался и бэктестом не подтверждён как рабочий рычаг,
  в отличие от глубины просадки.

rules_config REVOLVER (strategy_templates -- заводской дефолт, + все существующие
strategies):
- tactic_target_profit_pct: 15.0 -> 6.0
- tactic_trailing_stop_k: 11.0 -> 5.0 (tactic_stop_loss_k БЕЗ ИЗМЕНЕНИЙ, 6.0)
- idea_min_revenue_growth_pct: 0.00 (новый, na_or_check -- NULL не проваливает)
- idea_min_volume_ratio_20d: 1.0 (новый, na_or_check -- NULL не проваливает,
  поле новое -- до первого полного sync у всех тикеров будет NULL)
- portfolio_max_allowed_div_pct уже существует (1.5) -- теперь читается ЕЩЁ и на
  входе (_score_revolver), не только в постфактум-аудите (portfolio_inspector.py)

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) tickers.signal_volume_ratio_20d ...")
db_sys.execute_query("""
    ALTER TABLE public.tickers
    ADD COLUMN IF NOT EXISTS signal_volume_ratio_20d NUMERIC;
""")
print("   готово.")

print("2) tickers.signal_price_to_20d_high_pct ...")
db_sys.execute_query("""
    ALTER TABLE public.tickers
    ADD COLUMN IF NOT EXISTS signal_price_to_20d_high_pct NUMERIC;
""")
print("   готово.")

print("3) rules_config -- strategy_templates (заводской дефолт для будущих портфелей) ...")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || %s::jsonb
    WHERE system_key = 'REVOLVER'
    RETURNING system_key, rules_config;
""", ('{"tactic_target_profit_pct": 6.0, "tactic_trailing_stop_k": 5.0, '
      '"idea_min_revenue_growth_pct": 0.00, "idea_min_volume_ratio_20d": 1.0}',))
print(f"   {result}")

print("4) rules_config -- существующие strategies (все реальные Револьверные портфели) ...")
result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || %s::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'REVOLVER'
    RETURNING s.id, s.portfolio_id;
""", ('{"tactic_target_profit_pct": 6.0, "tactic_trailing_stop_k": 5.0, '
      '"idea_min_revenue_growth_pct": 0.00, "idea_min_volume_ratio_20d": 1.0}',))
print(f"   обновлено стратегий: {result}")

print("\nГотово.")
