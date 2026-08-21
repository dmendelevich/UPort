#!/usr/bin/env python3
"""
Тема «Сигнал D» (портфельный трейлинг-стоп прибыли, Claude/23_session_followups_2026-08-20.md):
- portfolios.peak_total_capital_usd -- бегущий all-time максимум total_capital портфеля
  (аналог assets.peak_price_since_entry, но на уровне портфеля), backfill текущим
  значением капитала для уже существующих портфелей (честный старт).
- portfolios.nav_daily_volatility_pct -- дневная волатильность NAV, пересчитывается
  по portfolio_value_history (cron_scheduler.py::snapshot_portfolio_values).
- portfolios.drawdown_alert_active / drawdown_alert_triggered_at -- состояние алерта
  прямо на портфеле, НЕ через public.alerts (та таблица требует NOT NULL listing_id/
  ticker -- заточена под конкретную бумагу, портфельное условие туда не ложится честно,
  см. обсуждение в сессии).
- public.portfolio_total_capital -- вычислительный view (по прецеденту strategy_exposure,
  Claude/02_universal_views.md принцип 4), одним SQL отдаёт total_capital_usd на
  портфель. Воспроизводит двухшаговую привязку счетов из
  PortfolioInspector._collect_raw_portfolio_facts -- НЕ джойнить accounts.portfolio_id
  напрямую, у накопительных (deposit) счетов он всегда NULL, только у торговых
  (проверено на живых данных 2026-08-21).
Разовый скрипт, идемпотентный, запускается один раз вручную.
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

print("1) portfolios.peak_total_capital_usd ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS peak_total_capital_usd NUMERIC;
""")
print("   готово.")

print("2) portfolios.nav_daily_volatility_pct ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS nav_daily_volatility_pct NUMERIC;
""")
print("   готово.")

print("3) portfolios.drawdown_alert_active / drawdown_alert_triggered_at ...")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS drawdown_alert_active BOOLEAN NOT NULL DEFAULT false;
""")
db_sys.execute_query("""
    ALTER TABLE public.portfolios
    ADD COLUMN IF NOT EXISTS drawdown_alert_triggered_at TIMESTAMP(0) WITHOUT TIME ZONE;
""")
print("   готово.")

print("4) public.portfolio_total_capital (view) ...")
db_sys.execute_query("DROP VIEW IF EXISTS public.portfolio_total_capital;")
db_sys.execute_query("""
    CREATE VIEW public.portfolio_total_capital AS
    WITH base_trade_accounts AS (
        SELECT portfolio_id, account_number
        FROM public.accounts
        WHERE account_type = 'trade' AND currency_id = 'USD' AND portfolio_id IS NOT NULL
    ),
    portfolio_cash AS (
        SELECT bta.portfolio_id,
               SUM(a.cash_available * COALESCE(c.multiplier, 1.0) * COALESCE(cr.rate, 1.0)) AS total_cash_usd
        FROM base_trade_accounts bta
        JOIN public.accounts a
            ON a.account_number IN (bta.account_number, 'D' || bta.account_number)
        LEFT JOIN public.currencies c ON c.id = a.currency_id
        LEFT JOIN public.currency_rates cr ON cr.from_currency = a.currency_id AND cr.to_currency = 'USD'
        GROUP BY bta.portfolio_id
    ),
    portfolio_assets AS (
        SELECT s.portfolio_id, SUM(se.exposure_usd) AS total_assets_usd
        FROM public.strategy_exposure se
        JOIN public.strategies s ON s.id = se.strategy_id AND s.is_active = true
        GROUP BY s.portfolio_id
    )
    SELECT
        p.id AS portfolio_id,
        COALESCE(pc.total_cash_usd, 0.0) + COALESCE(pa.total_assets_usd, 0.0) AS total_capital_usd
    FROM public.portfolios p
    LEFT JOIN portfolio_cash pc ON pc.portfolio_id = p.id
    LEFT JOIN portfolio_assets pa ON pa.portfolio_id = p.id;
""")
print("   готово.")

print("5) Бэкфилл peak_total_capital_usd текущим капиталом (честный старт, без ложного дродауна) ...")
result = db_sys.execute_query("""
    UPDATE public.portfolios p
    SET peak_total_capital_usd = ptc.total_capital_usd
    FROM public.portfolio_total_capital ptc
    WHERE ptc.portfolio_id = p.id AND p.peak_total_capital_usd IS NULL
    RETURNING p.id;
""")
print(f"   обновлено портфелей: {len(result) if isinstance(result, list) else 0}")

print("\nГотово.")
