#!/usr/bin/env python3
"""
BACKLOG.md №147 -- EDGX добавлена в пул легализации ФБ (utils.py:351/378) и в
TickerEvaluator.US_EXCHANGE_CODES (analytics_utils.py), но 17 тикеров, уже сидящих в
tickers с exchange_mic='EDGX' (CBOE -- компонент S&P500, + 16 фондов из манифеста),
были легализованы ДО фикса -- их ticker_name_map['FB']/['T212'] застыли на
'UNSUPPORTED', сегрегация отсекала их раньше похода к брокеру. Пул исправлен, но сами
строки сами себя не перелегализуют -- ensure_ticker_v3 бьёт мимо кэша только когда
raw-строка не совпадает с уже сохранённым кодом, а плоский повторный прогон новых
данных не запускается сам по себе. Разовый скрипт, дозаписывает актуальный код ФБ.
Идемпотентный (ensure_ticker_v3 сам решает, легализовывать заново или нет по кэшу).
"""
import sys
sys.path.append('/root/UPort')

from database import db_sys

symbols = db_sys.execute_query("SELECT symbol FROM public.tickers WHERE exchange_mic = 'EDGX' ORDER BY symbol;")
symbols = [row["symbol"] for row in (symbols or [])]
print(f"Найдено тикеров на EDGX: {len(symbols)} -> {symbols}")

for sym in symbols:
    ticker_id, listing_id = db_sys.ensure_ticker_v3(
        ticker_name_raw=sym,
        caller_role="MS",
        caller_id="EDGX_FIX_20260821",
        broker_id=1,
        fb_client=None,
    )
    row = db_sys.execute_row("SELECT ticker_name_map FROM public.tickers WHERE id = %s;", (ticker_id,))
    print(f"  {sym} (ticker_id={ticker_id}): {row.get('ticker_name_map') if row else None}")

print("\nГотово.")
