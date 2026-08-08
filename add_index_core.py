#!/usr/bin/env python3
"""
Тема «Индексное ядро» (Claude/15_index_core.md, Claude/BACKLOG.md №89):
- Новый strategy_templates: system_key=INDEX_CORE (VTI+VXUS+BND, целевые веса,
  порог сделки).
- Разовый бэкфилл строк strategies для всех существующих портфелей (тот же
  принцип универсального набора шаблонов, что и у остальных пяти -- BACKLOG.md
  №9/31): делит СЕГОДНЯШНЮЮ долю Консервативной 80/20 (ядро/стокпикинг) там, где
  Консервативная реально активна, иначе оба пассивны (0%).
- Разовая правка strategy_templates.recommended_share_pct у Консервативной
  (50 -> 10), чтобы будущие новые портфели тоже бутстрапились по новой философии
  (сам бутстрап всё равно заводит содержательные стратегии пассивными -- это
  только справочный ориентир, не меняет поведение существующих портфелей).
Разовый скрипт, запускается один раз вручную.
"""
import json
import sys
sys.path.append('/root/UPort')

from database import db_sys

INDEX_CORE_RULES_CONFIG = {
    "index_core_target_weights": {"VTI": 44.0, "VXUS": 36.0, "BND": 20.0},
    "index_core_min_trade_usd": 300.0,
}
HUMAN_PHILOSOPHY = (
    "Диверсифицированное ядро капитала -- весь рынок акций США (VTI) + весь рынок "
    "вне США (VXUS) + широкий облигационный индекс (BND), без отбора отдельных "
    "компаний. Вход -- без технического/фундаментального экрана, весь доступный "
    "кэш разом в самую недовешенную ногу. Выход -- сигнала «продай» не существует "
    "никогда, только пассивная ребалансировка направлением новых денег."
)

print("1) strategy_templates: INDEX_CORE ...")
existing = db_sys.execute_row("SELECT id FROM public.strategy_templates WHERE system_key = 'INDEX_CORE';")
if existing:
    template_id = existing["id"]
    print(f"   уже существует, id={template_id}, пропускаю создание.")
else:
    row = db_sys.execute_row(
        """
        INSERT INTO public.strategy_templates
            (template_name, rules_config, human_philosophy, system_key, recommended_share_pct)
        VALUES (%s, %s::jsonb, %s, %s, %s)
        RETURNING id;
        """,
        ("Индексное ядро", json.dumps(INDEX_CORE_RULES_CONFIG, ensure_ascii=False),
         HUMAN_PHILOSOPHY, "INDEX_CORE", 40.0)
    )
    template_id = row["id"]
    print(f"   создан, id={template_id}.")

print("2) strategy_templates: Консервативная recommended_share_pct 50 -> 10 (справочный ориентир бутстрапа) ...")
db_sys.execute_query(
    "UPDATE public.strategy_templates SET recommended_share_pct = 10.0 WHERE system_key = 'CONSERVATIVE_ACCUMULATION';"
)
print("   готово.")

print("3) Бэкфилл строк strategies для существующих портфелей без Индексного ядра ...")
portfolios = db_sys.execute_query("SELECT id, name FROM public.portfolios;") or []

for p in portfolios:
    p_id, p_name = p["id"], p["name"]
    already = db_sys.execute_row(
        "SELECT id FROM public.strategies WHERE portfolio_id = %s AND template_id = %s;",
        (p_id, template_id)
    )
    if already:
        print(f"   П{p_id} ({p_name}): уже есть, пропускаю.")
        continue

    cons_row = db_sys.execute_row(
        """
        SELECT s.id, s.strategy_share_pct, s.is_screening_active
        FROM public.strategies s JOIN public.strategy_templates st ON s.template_id = st.id
        WHERE s.portfolio_id = %s AND st.system_key = 'CONSERVATIVE_ACCUMULATION';
        """,
        (p_id,)
    )
    if not cons_row:
        print(f"   П{p_id} ({p_name}): нет строки Консервативной вообще -- пропускаю, не универсальный набор.")
        continue

    cons_share = float(cons_row["strategy_share_pct"] or 0.0)
    cons_active = bool(cons_row["is_screening_active"])

    if cons_active and cons_share > 0:
        core_share = round(cons_share * 0.8, 2)
        new_cons_share = round(cons_share * 0.2, 2)
        db_sys.execute_query(
            "UPDATE public.strategies SET strategy_share_pct = %s WHERE id = %s;",
            (new_cons_share, cons_row["id"])
        )
        is_screening_active = True
        print(f"   П{p_id} ({p_name}): Консервативная {cons_share}% -> {new_cons_share}%, ядро {core_share}% (активно).")
    else:
        core_share = 0.0
        is_screening_active = False
        print(f"   П{p_id} ({p_name}): Консервативная пассивна -- ядро тоже пассивно (0%).")

    db_sys.execute_query(
        """
        INSERT INTO public.strategies
            (portfolio_id, template_id, strategy_name, rules_config, human_philosophy,
             strategy_share_pct, is_active, is_screening_active)
        SELECT %s, st.id, st.template_name, st.rules_config, st.human_philosophy, %s, true, %s
        FROM public.strategy_templates st WHERE st.id = %s;
        """,
        (p_id, core_share, is_screening_active, template_id)
    )
    print(f"   П{p_id} ({p_name}): строка Индексного ядра создана.")

print("Готово.")
