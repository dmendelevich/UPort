#!/usr/bin/env python3
import sys
import os
import logging
from pathlib import Path

# 🔥 ЖЕЛЕЗНЫЙ АВТОМАТ ПУТЕЙ UPORT: поднимаемся на одну папку выше из site_connectors в корень
sys.path.append(str(Path(__file__).parent.parent.resolve()))

import yfinance as yf
import time

def sync_rates(db_instance):
    """
    Контур Б: Обновление макрокурсов валют Форекс к доллару США через Yahoo Finance.
    ОПТИМИЗИРОВАНО: Собирает только используемые валюты из tickers и listings.
    ВЫВЕРЕНО: Сохраняет оригинальный регистр кода (напр. GBp) для связей FK в СУБД.
    СТАНДАРТ: Фиксация времени строго по правилам UPort UTC TIMESTAMP(0).
    """
    logging.info("📡 [Yahoo Forex]: Сбор реально используемых валют из СУБД...")
    
    # Зрячий SQL-запрос: собираем только те валюты, которые есть в активах, исключая USD
    sql_used_currencies = """
        SELECT DISTINCT currency_id FROM public.tickers WHERE currency_id IS NOT NULL AND currency_id != 'USD'
        UNION
        SELECT DISTINCT currency_id FROM public.listings WHERE currency_id IS NOT NULL AND currency_id != 'USD';
    """
    db_res = db_instance.execute_query(sql_used_currencies)
    
    if not db_res:
        logging.info("ℹ️ [Yahoo Forex]: Используемых дополнительных валют в СУБД не обнаружено. Обновление не требуется.")
        return

    # Извлекаем оригинальные коды валют прямо из базы (сохраняя регистр, например 'GBp')
    currencies_to_update = [row['currency_id'].strip() for row in db_res]
    logging.info(f"📊 [Yahoo Forex]: Запуск точечного обновления для пар: {currencies_to_update} к USD")

    for code in currencies_to_update:
        try:
            # 🛡️ РАЗДЕЛЯЕМ РЕГИСТРЫ: Для Yahoo всегда UPPER, для базы — оригинал (code)
            code_for_yahoo = code.upper()
            logging.info(f"   • Запрос курса {code_for_yahoo} -> USD...")
            
            # ФИНАНСОВЫЙ МАППИНГ: Переводим архивный код RUR в понятный для Yahoo RUB
            yf_code = "RUB" if code_for_yahoo == "RUR" else code_for_yahoo
            
            # Формируем универсальный лаконичный тикер валютной пары
            pair = f"{yf_code}USD=X"
            
            ticker_obj = yf.Ticker(pair)
            raw_price = ticker_obj.fast_info['last_price']
            rate = float(raw_price)

            # Проверяем наличие записи, используя оригинальный code для составного PK (from_currency, to_currency)
            sql_check = f"""
                SELECT 1 FROM public.currency_rates 
                WHERE from_currency = '{code}' AND to_currency = 'USD' 
                LIMIT 1;
            """
            exists = db_instance.execute_query(sql_check)

            if exists:
                # Путь А: Строка есть — UPDATE с точным сохранением updated_at по UTC TIMESTAMP(0)
                sql_write = f"""
                    UPDATE public.currency_rates 
                    SET rate = {rate}, 
                        updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                    WHERE from_currency = '{code}' AND to_currency = 'USD';
                """
            else:
                # Путь Б: Строки нет — INSERT со строгим соблюдением Foreign Key СУБД
                sql_write = f"""
                    INSERT INTO public.currency_rates (from_currency, to_currency, rate, updated_at)
                    VALUES ('{code}', 'USD', {rate}, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0));
                """
            
            db_instance.execute_query(sql_write)
            logging.info(f"   ✅ Пара {code} -> USD успешно сохранена в СУБД. Курс: {rate:.6f}")
            
        except Exception as e:
            logging.error(f"   ❌ Ошибка обработки валютной пары {code}: {e}")
            
        time.sleep(0.5) # Защитная пауза от блокировок Yahoo

    logging.info("🏁 [Yahoo Forex]: Синхронизация валютных курсов завершена.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from database import db_sys
    sync_rates(db_sys)
