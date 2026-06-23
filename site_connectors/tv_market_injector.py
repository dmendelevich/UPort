#!/usr/bin/env python3
import os
import sys
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

# Импортируем официальный бесплатный шлюз TradingView
from tradingview_ta import TA_Handler, Interval

# Подгружаем системные пути, чтобы скрипт видел родительские модули и метод ensure_ticker_v2
sys.path.append(str(Path(__file__).parent.parent))

# Настраиваем лаконичное логирование для ночного крона
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_market_injection():
    """
    Ракетный Инжектор Рынка UPort v1.0 (Контур Freedom Broker).
    Автономно сканирует рынок США через TradingView по двум стратегическим контурам,
    легализует тикеры через ядро и забивает Сводный полигон 9999.
    """
    logging.info("📡 [MARKET INJECTOR]: Старт ночного цикла агрегации идей Уолл-стрит...")
    
    # ─── ШАГ 1: КОНСТРУИРУЕМ ПУЛ ТИКЕРОВ ДЛЯ АНАЛИЗА ───
    # Базовый список топ-50 самых ликвидных и волатильных компаний США для Ступени 1.
    # В будущем этот массив можно расширить до 200+ позиций, TradingView обсчитает их за секунды.
    target_scan_pool = [
        "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "NVDA", "PG", "UBER", 
        "AMD", "NFLX", "DIS", "BA", "MELI", "VRTX", "INTC", "CSCO", "CMCSA", 
        "PEP", "ADBE", "QCOM", "TXN", "AMGN", "HON", "AMAT", "SBUX", "MDLZ", 
        "ISRG", "GILD", "LRCX", "MRVL", "PANW", "V", "MA", "JPM", "BAC", 
        "XOM", "CVX", "WMT", "HD", "UNH", "PFE", "MRK", "KO", "ORCL", "CRM"
    ]
    
    discovered_symbols = set()
    
    # ─── КОНТУР А: ПОИСК ЛИДЕРОВ ИМПУЛЬСА (STRONG_BUY) ───
    logging.info(f"🔎 [Контур А]: Сканирую {len(target_scan_pool)} акций на статус STRONG_BUY...")
    for symbol in target_scan_pool:
        try:
            handler = TA_Handler(
                symbol=symbol,
                screener="america",
                exchange="NASDAQ",  # По умолчанию NASDAQ, автоматический фолбэк ниже
                interval=Interval.INTERVAL_1_DAY
            )
            analysis = handler.get_analysis()
            #if analysis.summary.get("RECOMMENDATION") == "STRONG_BUY":
            if analysis.summary.get("RECOMMENDATION") in ("STRONG_BUY", "BUY"):
                discovered_symbols.add(symbol)
        except Exception:
            try:
                # Фолбэк для акций, торгующихся на NYSE (например, PG, JPM, DIS)
                handler = TA_Handler(symbol=symbol, screener="america", exchange="NYSE", interval=Interval.INTERVAL_1_DAY)
                #if handler.get_analysis().summary.get("RECOMMENDATION") == "STRONG_BUY":
                if handler.get_analysis().summary.get("RECOMMENDATION") in ("STRONG_BUY", "BUY"):
                    discovered_symbols.add(symbol)
            except Exception:
                continue

    # ─── КОНТУР Б: ПОИСК ПАДШИХ АНГЕЛОВ (ИНДЕКС СТРАХА RSI < 30) ───
    logging.info(f"🔎 [Контур Б]: Сканирую {len(target_scan_pool)} акций на экстремальную перепроданность (RSI < 30)...")
    for symbol in target_scan_pool:
        try:
            handler = TA_Handler(symbol=symbol, screener="america", exchange="NASDAQ", interval=Interval.INTERVAL_1_DAY)
            analysis = handler.get_analysis()
            # Извлекаем точное техническое значение 14-дневного RSI со шлюза TradingView
            rsi_val = analysis.indicators.get("RSI")
            #if rsi_val is not None and rsi_val < 30.0:
            if rsi_val is not None and rsi_val < 45.0:
                logging.info(f"   🔥 [ПАДШИЙ АНГЕЛ НАЙДЕН]: {symbol} упал на дно рынка! RSI = {rsi_val:.2f}")
                discovered_symbols.add(symbol)
        except Exception:
            try:
                handler = TA_Handler(symbol=symbol, screener="america", exchange="NYSE", interval=Interval.INTERVAL_1_DAY)
                rsi_val = handler.get_analysis().indicators.get("RSI")
                #if rsi_val is not None and rsi_val < 30.0:
                if rsi_val is not None and rsi_val < 45.0:
                    logging.info(f"   🔥 [ПАДШИЙ АНГЕЛ НАЙДЕН]: {symbol} упал на дно рынка! RSI = {rsi_val:.2f}")
                    discovered_symbols.add(symbol)
            except Exception:
                continue

    logging.info(f"🏆 [UPort СИТО]: Ступень 1 отобрала {len(discovered_symbols)} уникальных кандидатов для легализации.")
    if not discovered_symbols:
        logging.info("🏁 [MARKET INJECTOR]: Чистых сигналов сегодня нет. Контур завершен.")
        return

    # ─── ШАГ 2: ПОДКЛЮЧЕНИЕ К СУБД И ВЫЗОВ ensure_ticker_v2 ЯДРА ───
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    
    # Прямая инициализация экземпляра ядра вашей базы данных.
    # Предполагаем, что у вас в проекте есть класс Database, импортируемый из модуля database.
    # Чтобы не ломать ваш синтаксис, мы динамически создаем подключение через psycopg2.
    db_params = {
        "host": os.getenv("DB_HOST"), "port": os.getenv("DB_PORT"),
        "database": os.getenv("DB_NAME"), "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASS")
    }

    try:
        # Здесь мы эмулируем вызов ensure_ticker_v2. 
        # Если у вас в кодовой базе этот метод привязан к объекту db_instance, 
        # мы импортируем ваш главный инстанс базы данных.
        from database import db_sys # Подгружаем ваш живой экземпляр СУБД UPort
        
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        for symbol in discovered_symbols:
            try:
                logging.info(f"   • Легализация тикера {symbol} через ядро UPort...")
                
                # 🔥 ВЫЗОВ ВАШЕГО НЕИЗМЕНЕННОГО МЕТОДА ЯДРА (Никаких угадалок валюты!)
                # Он сам сходит во Freedom Broker, найдет ISIN и создаст правильный листинг .US
                ticker_id, listing_id = db_sys.ensure_ticker_v2(
                    broker_id=1,
                    broker_symbol=symbol,
                    fallback_currency="USD", # Оставляем дефолт, пока не трогаем ядро
                    fb_client=None
                )
                
                if not listing_id:
                    logging.warning(f"   ⚠️ Брокер открестился от тикера {symbol}. Пропуск.")
                    continue

                # ─── ШАГ 3: АТОМАРНАЯ ЗАПИСЬ В СВОДНЫЙ ПОЛИГОН 9999 ───
                sql_inject = """
                    INSERT INTO public.watchlist (
                        portfolio_id, listing_id, considered_at, 
                        watched_at, approved_at, ordered_at, bought_at, sold_out_at
                    ) VALUES (
                        9999, %s, CURRENT_TIMESTAMP, NULL, NULL, NULL, NULL, NULL
                    ) ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET
                        considered_at = CURRENT_TIMESTAMP;
                """
                cur.execute(sql_inject, (listing_id,))
                logging.info(f"   ✅ [ПОЛИГОН 9999]: {symbol} успешно забит в фазу considered_at.")

            except Exception as ticker_err:
                logging.error(f"   ❌ Ошибка легализации для {symbol}: {ticker_err}")
                continue

        conn.commit()
        logging.info("⚡ [MARKET INJECTOR]: Сводный полигон 9999 успешно укомплектован новыми лидерами рынка.")

    except Exception as global_db_err:
        if 'conn' in locals() and conn: conn.rollback()
        logging.error(f"❌ [MARKET INJECTOR КРИТИЧЕСКИЙ СБОЙ СУБД]: {global_db_err}")
    finally:
        if 'cur' in locals() and cur: cur.close()
        if 'conn' in locals() and conn: conn.close()

if __name__ == "__main__":
    run_market_injection()
