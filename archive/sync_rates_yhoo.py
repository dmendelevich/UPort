import yfinance as yf
from datetime import datetime
import time

def sync_rates(db_instance):
    """
    Контур Б: Обновление макрокурсов валют Форекс к доллару США через Yahoo Finance.
    Вызывается размеренно раз в 2 часа по расписанию Cron или принудительно по кнопке.
    """
    print("📡 [Yahoo Forex]: Сбор уникальных валют из СУБД...")
    
    # Собираем все валюты, используемые в тикерах и базовых настройках пользователей
    sql_currencies = """
        SELECT DISTINCT id FROM (
            SELECT currency_id AS id FROM tickers
            UNION
            SELECT base_currency AS id FROM users
        ) AS all_currencies WHERE id != 'USD'
    """
    currencies_data = db_instance.execute_query(sql_currencies)
    
    if not currencies_data:
        print("ℹ️ [Yahoo Forex]: В системе не обнаружено дополнительных валют (кроме USD).")
        return

    currencies_to_update = [row['id'] for row in currencies_data]
    print(f"📊 [Yahoo Forex]: Запуск обновления для пар к USD: {currencies_to_update}")

    for code in currencies_to_update:
        try:
            print(f"   • Обновляю курс {code} -> USD...", end=" ")
            
            # Формируем тикер валютной пары для Yahoo (GBPUSD=X, EURUSD=X, для рубля RUB=X)
            pair = f"{code}USD=X" if code != 'RUB' else "RUB=X"
            
            ticker_obj = yf.Ticker(pair)
            raw_price = ticker_obj.fast_info['last_price']
            rate = float(raw_price)

            # Если это рубль (USDRUB), инвертируем курс, чтобы получить чистый RUB -> USD
            if code == 'RUB' and rate > 1:
                rate = 1 / rate

            # Записываем курс в таблицу currency_rates
            sql_insert_rate = f"""
                INSERT INTO currency_rates (from_currency, to_currency, rate, updated_at)
                VALUES ('{code}', 'USD', {rate}, '{datetime.now()}')
                ON CONFLICT (from_currency, to_currency) 
                DO UPDATE SET rate = EXCLUDED.rate, updated_at = EXCLUDED.updated_at;
            """
            db_instance.execute_query(sql_insert_rate)
            print(f"✅ {rate:.4f}")
            
        except Exception as e:
            print(f"❌ Ошибка пары {code}: {e}")
            
        time.sleep(0.5) # Легкая задержка, чтобы не спамить Yahoo
    print("🏁 [Yahoo Forex]: Синхронизация валютных курсов завершена.")
