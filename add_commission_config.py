#!/usr/bin/env python3
"""
Тема «Отдельный счёт для комиссий» (Claude/BACKLOG.md №88, открыт 2026-08-08,
реализация начата 2026-09-06) -- универсальный механизм комиссии для ВСЕХ
портфелей, разными путями по типу (см. Claude/BACKLOG.md, свежая запись):

- Реальные портфели (broker_id=1) -- комиссия НЕ хранится постоянно, это
  брокерский факт (принцип "снэпшот, не архив", CLAUDE.md) -- берётся на лету
  через FreedomBrokerClient.get_trades_history() (analytics/commission_report.py).
  Для них portfolios.commission_config не используется вообще.

- Бумажные портфели (broker_id IS NULL) -- никакого брокера нет, комиссия --
  это НАШЕ СОБСТВЕННОЕ решение, которое нигде больше не появится, если не
  посчитать и не записать самим (принцип "история собственных решений",
  тот же, что у order_pipelines) -- отсюда portfolios.commission_config (jsonb)
  + новая колонка public.orders.commission_usd, которую заполняет
  brokers_connectors/paper_broker.py в момент синтетического исполнения.

Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

import json
from database import db_sys

print("1) portfolios -- добавляю commission_config (jsonb) ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS commission_config JSONB;
""")

print("2) orders -- добавляю commission_usd (numeric, NULL для реальных ордеров -- заполняется только эмулятором) ...")
db_sys.execute_query("""
    ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS commission_usd NUMERIC;
""")

# Тариф П10 (0.12% от суммы сделки + $1.2 за приказ) -- тот же, что уже был
# зашит константой в experiments/revolver_hard_sltp_2026_09/backtest.py при
# калибровке параметров Револьверной (COMMISSION_RT_PCT) -- используем
# ОДИН И ТОТ ЖЕ тариф для всех 4 бумажных портфелей, не выдумываем новый.
TARIFF_P10 = json.dumps({"pct_of_trade": 0.12, "fixed_per_order_usd": 1.2, "monthly_fee_usd": 0})

print("3) Заполняю commission_config тарифом П10 для всех бумажных портфелей (broker_id IS NULL) ...")
paper_portfolios = db_sys.execute_query("SELECT id, name FROM public.portfolios WHERE broker_id IS NULL;")
for p in paper_portfolios:
    db_sys.execute_query(
        "UPDATE public.portfolios SET commission_config = %s::jsonb WHERE id = %s;",
        (TARIFF_P10, p["id"])
    )
    print(f"   -- {p['name']} (id={p['id']}) обновлён")

print("Готово.")
