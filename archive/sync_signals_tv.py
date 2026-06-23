# /root/UPort/site_connectors/sync_signals_tv.py
#!/usr/bin/env python3
import os
import sys
import time
import logging
from pathlib import Path

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

# Импортируем официальные инстансы, утилиты и конфигурации ядра
from database import db_sys
import utils
import config
import settings

# Импортируем проверенный шлюз TradingView
from tradingview_ta import TA_Handler, Interval

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def sync_tradingview_signals():
    print("\n" + "="*95)
    print("🌅 [UPort MORNING CRON v4.7]: Умное насыщение Master-таблицы сигналами TradingView...")
    print("="*95)

    # 🔥 ВЫГРУЖАЕМ ВСЕ 205 ЛИКВИДНЫХ БУМАГ (БОЕВОЙ РЕЖИМ, БЕЗ LIMIT)
    sql_get_universe = f"""
        SELECT t.id, t.symbol, ex.tv_code
        FROM public.tickers t
        JOIN public.exchanges ex ON t.exchange_mic = ex.mic
        WHERE t.daily_turnover_usd >= {settings.MIN_DAILY_TURNOVER_USD}
          AND ex.tv_code IS NOT NULL
          AND t.yahoo_symbol IS NOT NULL 
          AND t.yahoo_symbol != '';
    """
    
    logging.info("📡 Выгружаю полное ликвидное ядро рынка из СУБД UPort...")
    active_rows = db_sys.execute_query(sql_get_universe)
    
    if not active_rows:
        logging.warning(f"⚠️ Ликвидных бумаг с оборотом >= ${settings.MIN_DAILY_TURNOVER_USD:,} не обнаружено.")
        return

    logging.info(f"📊 Обнаружено {len(active_rows)} ликвидных инструментов. Запуск конвейера эшелонов...")
    total_updated = 0

    with utils.timer("Утренний технический скоринг TradingView"):
        # 🔥 ШАГ 2: РАЗВЕРТЫВАНИЕ СИСТЕМНОГО КОНВЕЙЕРА ЭШЕЛОНОВ
        for echelon_idx, db_batch in utils.create_echelons(active_rows, chunk_size=config.CHUNK_SIZE_TV):
            logging.info(f"🚀 [ЭШЕЛОН №{echelon_idx}]: Начинаю обработку пачки из {len(db_batch)} акций...")
            
            for row in db_batch:
                t_id = row["id"]
                symbol = row["symbol"]
                exchange = row["tv_code"]
                
                # Защита от бесконечного цикла при обработке ошибок
                retry_count = 0
                while retry_count < 2:
                    try:
                        # 🔥 ШАГ 3: ТОЧЕЧНЫЙ ОПРОС ПО СУБД-СПРАВОЧНИКУ TV_CODE
                        handler = TA_Handler(
                            symbol=symbol,
                            screener=settings.TV_DEFAULT_SCREENER,
                            exchange=exchange,
                            interval=Interval.INTERVAL_1_DAY,
                            timeout=config.NETWORK_TIMEOUT_SEC
                        )
                        
                        analysis = handler.get_analysis()
                        if not analysis or not analysis.indicators:
                            break
                            
                        indicators = analysis.indicators
                        summary = analysis.summary

                        # 🔥 ШАГ 4: ИЗВЛЕЧЕНИЕ МЕТРИК И РАСЧЕТ ТРЕНДА
                        recommendation = summary.get("RECOMMENDATION", "NEUTRAL")
                        rsi_val = indicators.get("RSI")
                        
                        macd_line = indicators.get("MACD.macd")
                        macd_signal = indicators.get("MACD.signal")
                        macd_status = "NEUTRAL"
                        if macd_line is not None and macd_signal is not None:
                            macd_status = "BUY" if macd_line > macd_signal else "SELL"

                        ema20 = indicators.get("EMA20")
                        sma50 = indicators.get("SMA50")
                        sma200 = indicators.get("SMA200")
                        close_price = indicators.get("close")
                        
                        price_to_sma200_pct = "NULL"
                        if close_price and sma200:
                            calc_pct = ((float(close_price) / float(sma200)) - 1) * 100
                            price_to_sma200_pct = f"{calc_pct:.2f}"

                        rsi_sql = f"{rsi_val:.2f}" if rsi_val is not None else "NULL"
                        ema20_sql = f"{ema20:.4f}" if ema20 is not None else "NULL"
                        sma50_sql = f"{sma50:.4f}" if sma50 is not None else "NULL"
                        sma200_sql = f"{sma200:.4f}" if sma200 is not None else "NULL"

                        # 🔥 ШАГ 5: ТОЧЕЧНЫЙ АПДЕЙТ КОЛОНОК ТАБЛИЦЫ TICKERS
                        sql_update = f"""
                            UPDATE public.tickers
                            SET 
                                tv_recommendation = '{recommendation}',
                                tv_rsi = {rsi_sql},
                                tv_macd_signal = '{macd_status}',
                                tv_ema_20 = {ema20_sql},
                                tv_sma_50 = {sma50_sql},
                                tv_sma_200 = {sma200_sql},
                                tv_price_to_sma200_pct = {price_to_sma200_pct},
                                tv_signals_last_synced_at = CURRENT_TIMESTAMP
                        WHERE id = {t_id};
                    """
                        db_sys.execute_query(sql_update)
                        total_updated += 1
                        break  # Успешно выполнено, выходим из цикла retry

                    except Exception as err:
                        err_str = str(err)
                        if "429" in err_str:
                            logging.warning(f"   🚨 [STATUS 429]: Превышен лимит запросов на {symbol}. Сплю 30 секунд...")
                            time.sleep(30)
                            retry_count += 1
                        else:
                            logging.warning(f"   ⚠️ Сбой обработки тикера {symbol} на бирже {exchange}: {err}")
                            break
                    
                    finally:
                        # Базовая безопасная пауза между акциями
                        time.sleep(config.PAUSE_TV_SEC)
                
            logging.info(f"   ✅ [ЭШЕЛОН №{echelon_idx} ЗАВЕРШЕН]: Успешно синхронизировано инструментов: {total_updated} шт.")
            
            # 🔥 УМНЫЙ РАЗГРУЗОЧНЫЙ КЛАПАН: Глубокий вдох для IP-адреса между крупными пачками
            if total_updated < len(active_rows):
                logging.info("   ⏳ [UPort COOL DOWN]: Даю вашему IP-адресу отдохнуть 15 секунд перед следующим эшелоном...")
                time.sleep(15)

    print("\n" + "="*95)
    print(f"🏁 [UPort MORNING CRON COMPLETE]: Пакетный модуль наполнил данными: {total_updated} шт.!")
    print("="*95 + "\n")

if __name__ == "__main__":
    sync_tradingview_signals()
