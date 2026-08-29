#!/usr/bin/env python3
"""
Тема «Терпение выхода Револьверной» (Claude/BACKLOG.md, 2026-08-28) -- бэктест
экрана входа/выхода на 30 ликвидных тикерах (2015-2026) показал: снижение цели
(15%->5%) почти не меняет средний результат (96% сделок вообще не доживают ни
до какой цели), а расширение TREND_REVERSAL_CONFIRM_DAYS с 3 до 10-12 дней даёт
заметный, монотонный прирост (средний результат +0.03%->+0.91%, доля выбитых
"сломом" 96%->43-52%).

TREND_REVERSAL_CONFIRM_DAYS=3 (settings.py) -- общая константа на все три
содержательные стратегии; менять её напрямую задело бы уже откалиброванную
Трендовую. Вместо этого -- новый параметр rules_config `tactic_confirm_days`,
читается ТОЛЬКО в _score_revolver (analytics_utils.py) и _check_revolver_exit
(position_exit_evaluator.py), с откатом на settings.TREND_REVERSAL_CONFIRM_DAYS,
если не задан -- у Трендовой/Консервативной остаётся прежнее поведение.

Значение -- 12 (не 10): в бэктесте оба дали одинаковый средний результат
(+0.91%), но 12 давал заметно лучшую долю прибыльных сделок (50.7% против
46.6%) и меньшую долю ложных "сломов" (43% против 52%).

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) tactic_confirm_days -- strategy_templates (заводской дефолт для будущих портфелей) ...")
result = db_sys.execute_query("""
    UPDATE public.strategy_templates
    SET rules_config = rules_config || '{"tactic_confirm_days": 12}'::jsonb
    WHERE system_key = 'REVOLVER'
    RETURNING system_key;
""")
print(f"   {result}")

print("2) tactic_confirm_days -- существующие strategies (все реальные портфели) ...")
result = db_sys.execute_query("""
    UPDATE public.strategies s
    SET rules_config = s.rules_config || '{"tactic_confirm_days": 12}'::jsonb
    FROM public.strategy_templates st
    WHERE s.template_id = st.id AND st.system_key = 'REVOLVER'
    RETURNING s.id, s.portfolio_id;
""")
print(f"   обновлено стратегий: {result}")

print("\nГотово.")
