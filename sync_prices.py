import yfinance as yf
import psycopg2
from datetime import datetime
import time

DB_PARAMS = {
    "host": "localhost",
    "database": "uport_db",
    "user": "uport_admin",
    "password": "uport_admin_pw"
}

def update_all():
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor()

        # --- 1. ОБНОВЛЕНИЕ ЦЕН ТИКЕРОВ ---
        cur.execute("""
            SELECT t.id, t.symbol, c.multiplier 
            FROM tickers t 
            JOIN currencies c ON t.currency_id = c.id
        """)
        tickers_data = cur.fetchall()

        for t_id, symbol, multiplier in tickers_data:
            # Для Berkshire заменяем точку на дефис
            yf_symbol = symbol.replace('.', '-')
            try:
                print(f"Обновляю тикер {symbol}...", end=" ")
                t = yf.Ticker(yf_symbol)
                # Берем быструю цену (last_price)
                raw_price = t.fast_info['last_price']
                
                if raw_price and not str(raw_price) == 'nan':
                    # Умножаем на multiplier (напр. 0.01 для пенсов)
                    final_price = float(raw_price) * float(multiplier)
                    cur.execute("""
                        UPDATE tickers 
                        SET last_price = %s, last_updated_at = %s 
                        WHERE id = %s
                    """, (final_price, datetime.now(), t_id))
                    print(f"✅ {final_price:.2f}")
                else:
                    print("⚠️ Нет данных")
            except Exception as e:
                print(f"❌ Ошибка: {e}")
            time.sleep(0.5)

        # --- 2. ОБНОВЛЕНИЕ КУРСОВ ВАЛЮТ ---
        # Выбираем все валюты, которые используются в системе (кроме USD)
        cur.execute("""
            SELECT DISTINCT id FROM (
                SELECT currency_id AS id FROM tickers
                UNION
                SELECT base_currency AS id FROM users
            ) AS all_currencies WHERE id != 'USD'
        """)
        currencies_to_update = [row[0] for row in cur.fetchall()]

        for code in currencies_to_update:
            try:
                print(f"Обновляю курс {code} -> USD...", end=" ")
                # Формируем тикер валютной пары для Yahoo
                # Для большинства это CODEUSD=X (GBPUSD=X, EURUSD=X)
                # Для рубля Yahoo чаще дает USDRUB=X
                pair = f"{code}USD=X" if code != 'RUB' else "RUB=X"
                
                data = yf.Ticker(pair).fast_info['last_price']
                rate = float(data)

                # Если это рубль (USDRUB), инвертируем курс, чтобы получить RUB -> USD
                if code == 'RUB' and rate > 1:
                    rate = 1 / rate

                cur.execute("""
                    INSERT INTO currency_rates (from_currency, to_currency, rate, updated_at)
                    VALUES (%s, 'USD', %s, %s)
                    ON CONFLICT (from_currency, to_currency) 
                    DO UPDATE SET rate = EXCLUDED.rate, updated_at = EXCLUDED.updated_at
                """, (code, rate, datetime.now()))
                print(f"✅ {rate:.4f}")
            except Exception as e:
                print(f"❌ Ошибка валюты {code}: {e}")

        conn.commit()
        cur.close()
        conn.close()
        print("\n🏁 Синхронизация завершена успешно.")

    except Exception as e:
        print(f"❌ Системная ошибка: {e}")

if __name__ == "__main__":
    update_all()
