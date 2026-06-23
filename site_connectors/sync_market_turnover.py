#!/usr/bin/env python3
import sys
import logging
import time
from pathlib import Path
import yfinance as yf
import pandas as pd

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

# Импортируем ваш официальный авторизованный инстанс базы из ядра
from database import db_sys
# Импортируем наши новые глобальные утилиты UPort
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def sync_all_universe_turnover():
    print("\n" + "="*95)
    print("🌙 [UPort NIGHTLY CRON v3.1]: Тотальное Сито Оборотов Master-таблицы TICKERS...")
    print("="*95)
    
    # СНАЙПЕРСКИЙ РЕЛЯЦИОННЫЙ ВЫБОР:
    sql_get = """
        SELECT symbol, yahoo_symbol 
        FROM public.tickers 
        WHERE (provenance ? 'MS_SP500' OR provenance ? 'MS_TOP100_ETF')
          AND yahoo_symbol IS NOT NULL 
          AND yahoo_symbol != '';
    """
    
    logging.info("📡 Выгружаю целевой массив тикеров из Master-таблицы public.tickers...")
    rows = db_sys.execute_query(sql_get)
    
    if not rows:
        logging.warning("⚠️  В Master-таблице tickers не обнаружено размеченных системных тикеров. Проверьте Сканер!")
        return
        
    # Формируем карту обратного маппинга
    ticker_map = {row["yahoo_symbol"]: row["symbol"] for row in rows}
    all_yahoo_symbols = list(ticker_map.keys())
    
    logging.info(f"📊 В ядре обнаружено {len(all_yahoo_symbols)} системных инструментов. Нарезаю Chunker-ом на эшелоны по 100...")
    
    chunk_index = 0
    total_updated = 0
    
    with utils.timer("Еженощный обсчет долларовой ликвидности ядра public.tickers"):
        # Нарезаем массив утилитой utils.chunk_array на пачки по 100 штук
        for db_batch in utils.chunk_array(all_yahoo_symbols, chunk_size=100):
            chunk_index += 1
            logging.info(f"   📦 [ЭШЕЛОН №{chunk_index}]: Заправляю в Yahoo пачку из {len(db_batch)} тикеров...")
            
            try:
                # Скачиваем итоги последней закрытой сессии пачкой за 1 клик
                data = yf.download(db_batch, period="1d", group_by="ticker", progress=False, timeout=25)
                
                if data.empty:
                    continue

                updated_count = 0
                
                # Перебираем тикеры строго по нашему скалярному эталону без проверки data.columns.levels
                for yf_sym in db_batch:
                    try:
                        ticker_data = data[yf_sym]
                        
                        close_series = ticker_data['Close'].dropna()
                        volume_series = ticker_data['Volume'].dropna()
                        
                        if close_series.empty or volume_series.empty:
                            continue
                            
                        # Извлекаем последнее скалярное число из вектора сессии
                        close_price = float(close_series.iloc[-1])
                        volume = int(volume_series.iloc[-1])
                        
                        if volume <= 0 or close_price <= 0:
                            continue
                            
                        fb_symbol = ticker_map[yf_sym]
                        
                        # МОНОЛИТНЫЙ SQL-АПДЕЙТ ЯДРА: Наливаем доллары напрямую в tickers!
                        sql_update = f"""
                            UPDATE public.tickers t
                            SET 
                                daily_turnover_usd = ROUND({volume} * {close_price} * c.multiplier * r.rate),
                                turnover_last_synced_at = CURRENT_TIMESTAMP
                            FROM public.exchanges ex
                            JOIN public.currencies c ON ex.currency_id = c.id
                            JOIN public.currency_rates r ON c.id = r.from_currency
                            WHERE t.exchange_mic = ex.mic 
                              AND t.symbol = '{fb_symbol}' 
                              AND r.to_currency = 'USD';
                        """
                        db_sys.execute_query(sql_update)
                        updated_count += 1
                        total_updated += 1
                        
                    except Exception:
                        # Если конкретный тикер сбоит внутри Pandas, мы изолированно пропускаем только его
                        continue
                        
                logging.info(f"   ✅ [ЭШЕЛОН №{chunk_index} ВЫПОЛНЕН]: Мультивалютно обсчитано и записано: {updated_count} шт.")
                
            except Exception as chunk_err:
                logging.error(f"   ❌ Ошибка при обработке Эшелона №{chunk_index}: {chunk_err}")
                
            # Вежливая защитная пауза в 1.5 секунды для полной блокировки банов IP
            time.sleep(1.5)
            
    print("\n" + "="*95)
    print(f"🏁 [NIGHTLY CRON COMPLETE]: Всего в public.tickers ликвидностью размечено: {total_updated} шт.!")
    print("="*95 + "\n")

if __name__ == "__main__":
    sync_all_universe_turnover()
