#!/usr/bin/env python3
"""
Тема «Счета/ИТОГО/Сводка капитала» (2026-08-14):
- brokers.flag_emoji -- явный флаг для заголовка секции брокера на экране «Счета»
  (не выводится из brokers.country -- это не строгий ISO-код, 'UK' не 'GB').
- v_accounts_full -- Слой 1 (чистый JOIN, без бизнес-логики, по конвенции
  Claude/02_universal_views.md) -- одна строка = одна строка accounts, с именами
  владельца/портфеля/брокера и знаком валюты. LEFT JOIN portfolios/brokers --
  накопительный счёт (portfolio_id=NULL) и счёт бумажного портфеля «ПБум»
  (broker_id=NULL) должны попадать в view, не отфильтровываться JOIN'ом.
Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) brokers.flag_emoji ...")
db_sys.execute_query("""
    ALTER TABLE public.brokers
    ADD COLUMN IF NOT EXISTS flag_emoji VARCHAR(8);
""")
db_sys.execute_query("UPDATE public.brokers SET flag_emoji = '🇰🇿' WHERE short_name = 'FB';")
db_sys.execute_query("UPDATE public.brokers SET flag_emoji = '🇬🇧' WHERE short_name = 'T212';")
print("   готово:", db_sys.execute_query("SELECT short_name, flag_emoji FROM public.brokers;"))

print("2) v_accounts_full ...")
db_sys.execute_query("DROP VIEW IF EXISTS public.v_accounts_full;")
db_sys.execute_query("""
    CREATE VIEW public.v_accounts_full AS
    SELECT
        a.id AS account_id,
        a.user_id,
        a.portfolio_id,
        a.broker_id,
        a.account_number,
        a.account_type,
        a.currency_id,
        a.cash_available,
        a.cash_reserved,
        a.assets_value,
        a.last_updated AS account_last_updated,
        u.name AS owner_name,
        p.name AS portfolio_name,
        br.name AS broker_name,
        br.short_name AS broker_short_name,
        br.flag_emoji AS broker_flag_emoji,
        cur.sign AS currency_sign
    FROM public.accounts a
    JOIN public.users u ON a.user_id = u.id
    LEFT JOIN public.portfolios p ON a.portfolio_id = p.id
    LEFT JOIN public.brokers br ON a.broker_id = br.id
    JOIN public.currencies cur ON a.currency_id = cur.id;
""")
print("   готово.")

print("\nПроверка -- накопительный П10 и счёт «ПБум» должны присутствовать:")
print(db_sys.execute_query("""
    SELECT account_id, owner_name, portfolio_name, broker_name, broker_flag_emoji, account_type, currency_id, cash_available
    FROM public.v_accounts_full
    WHERE (user_id = 1 AND account_type = 'deposit') OR portfolio_id = 8
    ORDER BY account_id;
"""))

print("\nГотово.")
