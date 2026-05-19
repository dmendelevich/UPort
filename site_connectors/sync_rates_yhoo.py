import yfinance as yf
from datetime import datetime
import time

def sync_rates(db_instance):
    """
    Контур Б: Обновление макрокурсов валют Форекс к доллару США через Yahoo Finance.
    ИСПРАВЛЕНО: Запрос переведен на чистый справочник public.currencies и добавлен маппинг RUR->RUB.
    """
    print("📡 [Yahoo Forex]: Сбор валют из системного справочника public.currencies...")
    
    # Идеальный, легкий запрос к СУБД строго по вашему плану
    sql_currencies = "SELECT id FROM public.currencies WHERE id != 'USD';"
    currencies_data = db_instance.execute_query(sql_currencies)
    
    if not currencies_data:
        print("ℹ️ [Yahoo Forex]: В справочнике currencies не обнаружено дополнительных валют.")
        return

    # Извлекаем чистые строковые коды валют
    currencies_to_update = [row['id'].upper() for row in currencies_data]
    print(f"📊 [Yahoo Forex]: Запуск обновления для пар к USD: {currencies_to_update}")

    for code in currencies_to_update:
        try:
            print(f"   • Обновляю курс {code} -> USD...", end=" ")
            
            # ФИНАНСОВЫЙ МАППИНГ: Переводим архивный код RUR в понятный для Yahoo RUB
            yf_code = "RUB" if code == "RUR" else code
            
            # Формируем тикер валютной пары для Yahoo Finance
            pair = f"{yf_code}USD=X" if yf_code != 'RUB' else "RUB=X"
            
            ticker_obj = yf.Ticker(pair)
            raw_price = ticker_obj.fast_info['last_price']
            rate = float(raw_price)

            # Если это рубль (RUB=X), Yahoo возвращает курс доллара к рублю (напр. 91.5).
            # Инвертируем его, чтобы получить чистую стоимость одного рубля в USD
            if yf_code == 'RUB' and rate > 1:
                rate = 1 / rate

            # Записываем курс в таблицу currency_rates под ОРИГИНАЛЬНЫМ кодом 'code' (RUR или RUB)
            sql_insert_rate = f"""
                INSERT INTO public.currency_rates (from_currency, to_currency, rate, updated_at)
                VALUES ('{code}', 'USD', {rate}, '{datetime.now()}')
                ON CONFLICT (from_currency, to_currency) 
                DO UPDATE SET rate = EXCLUDED.rate, updated_at = EXCLUDED.updated_at;
            """
            db_instance.execute_query(sql_insert_rate)
            print(f"✅ {rate:.6f}")
            
        except Exception as e:
            print(f"❌ Ошибка пары {code}: {e}")
            
        time.sleep(0.5) # Защитная пауза
    print("🏁 [Yahoo Forex]: Синхронизация валютных курсов завершена.")
