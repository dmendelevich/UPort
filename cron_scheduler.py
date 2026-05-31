import asyncio
import logging
from datetime import datetime

# Напрямую импортируем наши изолированные рабочие утилиты
from site_connectors.sync_rates_yhoo import sync_rates
from site_connectors.sync_fundamentals_yhoo import sync_fundamentals
from brokers_connectors.sync_quotes_fb import sync_quotes_fb_branch
from brokers_connectors.sync_quotes_t212 import sync_quotes_t212_branch
from brokers_connectors.fb_client import FreedomBrokerClient

# Импортируем нашу глобальную асинхронную очередь задач из ядра
from database import ETF_LOOK_THROUGH_QUEUE

# ─────────────────────────────────────────────────────────
# === ГЛОБАЛЬНЫЕ НАСТРОЙКИ ПЕРИОДИЧНОСТИ (В СЕКУНДАХ) ===
# ─────────────────────────────────────────────────────────
QUOTES_SYNC_INTERVAL = 900          # 15 минут для котировок акций
RATES_SYNC_INTERVAL = 7200          # 2 часа для макрокурсов валют Форекс
FUNDAMENTALS_CHECK_INTERVAL = 3600  # 1 час для проверки ночного реле фундаментала
FUNDAMENTALS_TARGET_HOUR = 3        # Целевой час запуска ночного анализа (03:00 ночи)
ETF_WORKER_PAUSE = 5                # Дыхательная пауза воркера между разбором фондов (секунд)


# === НАПРЯМЫЕ ФУНКЦИИ ОБНОВЛЕНИЯ ===

def run_quotes_update(db_instance):
    """Сбор тикеров из базы и точечный вызов REST-коннекторов брокеров с фильтром tracking_status."""
    logging.info("⚡ Прямой запуск обновления котировок акций...")
    
    all_brokers = db_instance.execute_query("SELECT id, name FROM public.brokers;")
    if not all_brokers:
        logging.warning("[Cron]: Справочник brokers пуст.")
        return

    for broker in all_brokers:
        b_id = int(broker['id'])
        b_name = broker['name']
        
        # 🔥 АКАДЕМИЧЕСКИЙ ЗАПРОС v3.0: Собираем листинги на основе живого watchlist портфелей этого брокера
        sql_tickers = f"""
            SELECT DISTINCT ON (l.broker_symbol) l.id AS listing_id, l.broker_symbol AS full_ticker, c.multiplier 
            FROM public.watchlist w
            JOIN public.listings l ON w.listing_id = l.id
            JOIN public.portfolios p ON w.portfolio_id = p.id
            JOIN public.currencies c ON l.currency_id = c.id
            WHERE p.broker_id = {b_id} AND w.status IN ('considered', 'ordered', 'bought', 'sold_out');
        """
        tickers_data = db_instance.execute_query(sql_tickers)
        if not tickers_data:
            continue
            
        logging.info(f"💼 [Cron]: Обновляю {len(tickers_data)} активных листингов у брокера {b_name} (ID: {b_id})")
        
        if b_id == 1:
            sync_quotes_fb_branch(tickers_data, db_instance, FreedomBrokerClient)
        elif b_id == 2:
            sync_quotes_t212_branch(tickers_data, db_instance)

        if not tickers_data:
            continue
            
        logging.info(f"💼 [Cron]: Обновляю {len(tickers_data)} бумаг у брокера {b_name} (ID: {b_id})")
        
        if b_id == 1:
            sync_quotes_fb_branch(tickers_data, db_instance, FreedomBrokerClient)
        elif b_id == 2:
            sync_quotes_t212_branch(tickers_data, db_instance)

def run_rates_update(db_instance):
    """Прямой вызов сбора Форекс-курсов через Yahoo Finance."""
    logging.info("⚡ Прямой запуск обновления курсов валют...")
    sync_rates(db_instance)


# === ПЕТЛИ ВРЕМЕНИ (РЕЛЕ) ===

async def quotes_clock_loop(db_instance):
    """Реле времени цен акций (раз в 15 минут)"""
    logging.info("⏱️ [Реле Времени]: Контур цен акций взведен.")
    while True:
        try:
            await asyncio.to_thread(run_quotes_update, db_instance)
        except Exception as e:
            logging.error(f"❌ [Cron Цен Error]: {e}")
        await asyncio.sleep(QUOTES_SYNC_INTERVAL)

async def rates_clock_loop(db_instance):
    """Реле времени макрокурсов валют (раз в 2 часа)"""
    logging.info("⏱️ [Реле Времени]: Контур курсов валют успешно взведен.")
    while True:
        try:
            await asyncio.to_thread(run_rates_update, db_instance)
        except Exception as e:
            logging.error(f"❌ [Cron Валют Error]: {e}")
        await asyncio.sleep(RATES_SYNC_INTERVAL)

async def fundamentals_clock_loop(db_instance):
    """УМНОЕ НОЧНОЕ РЕЛЕ ВРЕМЕНИ: Просыпается раз в час."""
    logging.info("⏱️ [Реле Времени]: Контур фундаментального анализа акций УОЛЛ-СТРИТ взведен.")
    is_first_start = False
    
    while True:
        try:
            current_hour = datetime.now().hour
            if current_hour == FUNDAMENTALS_TARGET_HOUR or is_first_start:
                logging.info(f"🌙 [Cron]: На часах {FUNDAMENTALS_TARGET_HOUR:02d}:00 ночи. Запуск планового анализа...")
                await asyncio.to_thread(sync_fundamentals, db_instance)
                is_first_start = False
        except Exception as e:
            logging.error(f"❌ [Cron Фундаментал Error]: {e}")
            
        await asyncio.sleep(FUNDAMENTALS_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────
# === НОВАЯ ПЕТЛЯ: ОДНОПОТОЧНЫЙ ВОРКЕР ОЧЕРЕДИ ETF ===
# ─────────────────────────────────────────────────────────
async def etf_queue_worker_loop(db_instance):
    """
    Интеллектуальный линейный конвейер UPort:
    Поочередно достает любые задачи из очереди, проверяет тип актива через Yahoo.
    Если это обычная акция — проскакивает разбор и засыпает на 1 секунду.
    Если это фонд (ETF) — делает полную Look-Through декомпозицию структуры.
    """
    logging.info("⏱️ [Реле Времени]: Однопоточный воркер очереди ETF Look-Through взведен на дежурство.")
    import yfinance as yf
    import pandas as pd
    
    while True:
        task = await ETF_LOOK_THROUGH_QUEUE.get()
        t_id = task["id"]
        symbol = task["symbol"]
        suffix = task["suffix"]
        full_ticker = task["full_ticker"]
        currency_id = task["currency_id"]
        broker_id = task["broker_id"]
        
        # Динамическая пауза: по умолчанию 1 секунда для обычных акций, чтобы беречь API
        current_worker_pause = 1.0
        
        try:
            yf_symbol = symbol.replace('.', '-') if symbol else symbol
            ticker_obj = yf.Ticker(yf_symbol)
            
            # Запрашиваем профиль info в асинхронном потоке
            info = await asyncio.to_thread(lambda: ticker_obj.info)
            
            q_type = info.get('quoteType', 'Unknown') if info else 'Unknown'
            is_etf = (q_type == 'ETF' or (info and info.get('expenseRatio') is not None))
            
            if not is_etf:
                # Это обычная акция (AAPL, AMD и т.д.)
                # Проскакиваем сложные вопросы, фиксируем микро-паузу в 1 сек и идем дальше
                logging.info(f"🍃 [Очередь]: Инструмент {full_ticker} является акцией ({q_type}). Проскакиваю Look-Through анализ.")
                current_worker_pause = 1.0
            else:
                # Это реальный фонд (ETF)! Включаем полную мощность декомпозиции
                logging.info(f"🧱 [Очередь ETF]: Обнаружен фонд {full_ticker}. Начинаю сквозной разбор структуры...")
                current_worker_pause = float(ETF_WORKER_PAUSE) # Переключаемся на стандартные 5 секунд для фондов
                
                holdings_df = None
                if hasattr(ticker_obj, 'funds_data') and ticker_obj.funds_data and hasattr(ticker_obj.funds_data, 'top_holdings'):
                    holdings_df = ticker_obj.funds_data.top_holdings
                elif isinstance(info.get('holdings'), list):
                    holdings_df = pd.DataFrame(info.get('holdings'))

                if holdings_df is not None and hasattr(holdings_df, 'empty') and not holdings_df.empty:
                    holdings_df = holdings_df.reset_index()
                    holdings_data = holdings_df.to_dict(orient='records')
                else:
                    holdings_data = []

                if holdings_data:
                    for comp in holdings_data:
                        # Используем точные ключи, которые подтвердил наш дебаг-принт!
                        comp_sym = comp.get('Symbol') or comp.get('symbol') or comp.get('index') or comp.get('Ticker')
                        comp_name = comp.get('Name') or comp.get('name') or "Unknown"
                        
                        raw_weight = comp.get('Holding Percent') or comp.get('holdingPercent') or comp.get('weight') or comp.get('Value') or 0
                        weight = float(raw_weight)
                        if 0 < weight < 1.0: 
                            weight = weight * 100
                        
                        if not comp_sym or weight == 0:
                            continue
                            
                        # 🔥 ВОРКЕР ETF v3.7: Компонент от Yahoo изначально глобален (напр. ANTO.L)
                        # Передаем его в ядро как есть, без принудительной склейки суффикса .US
                        comp_global_symbol = str(comp_sym).strip().upper()
                        
                        # 🔥 КИТ v3.7: Рекурсивно создаем стерильный тикер в базе и получаем IDs.
                        # Так как fb_client в планировщике недоступен, передаем None — 
                        # если компонента нет в базе, он создастся по дефолту с чистым символом (ANTO.L)
                        comp_id, comp_listing_id = await asyncio.to_thread(
                            db_instance.ensure_ticker_v2,
                            broker_id=broker_id,
                            broker_symbol=comp_global_symbol,
                            fallback_currency=currency_id,
                            fb_client=None
                        )
                        
                        if not comp_id:
                            continue
                        
                        # Мягко обогащаем название компании в tickers строго по числовому ID
                        if comp_name != "Unknown":
                            clean_comp_name = comp_name.replace("'", "")
                            sql_update_name = f"""
                                UPDATE public.tickers 
                                SET company_name = '{clean_comp_name}' 
                                WHERE id = {comp_id} AND (company_name IS NULL OR company_name = '' OR company_name = 'Unknown Company');
                            """
                            await asyncio.to_thread(db_instance.execute_query, sql_update_name)

                        # Записываем связь в etf_holdings по чистым глобальным ID
                        sql_insert_link = f"""
                            INSERT INTO public.etf_holdings (etf_ticker_id, component_ticker_id, weight_percentage)
                            VALUES ({t_id}, {comp_id}, {weight})
                            ON CONFLICT (etf_ticker_id, component_ticker_id) 
                            DO UPDATE SET weight_percentage = EXCLUDED.weight_percentage, last_updated_at = CURRENT_TIMESTAMP;
                        """
                        await asyncio.to_thread(db_instance.execute_query, sql_insert_link)

                        
                    logging.info(f"✅ [Очередь ETF Успех]: Структура фонда {full_ticker} успешно разобрана конвейером.")
                else:
                    logging.warning(f"⚠️ [Очередь ETF]: Yahoo не вернул внутренние холдинги для фонда {full_ticker}.")
            
        except Exception as e:
            logging.error(f"❌ [Очередь ETF Сбой] Ошибка обработки {full_ticker} воркером: {e}")
            
        # Освобождаем задачу в asyncio.Queue
        ETF_LOOK_THROUGH_QUEUE.task_done()
        # Динамический сон: 1 секунда для акций или 5 секунд для фондов
        await asyncio.sleep(current_worker_pause)



async def start_clocks(db_instance):
    """Точка старта реле времени фоновых задач проекта UPort"""
    logging.info("🧠 [Реле Времени]: Запуск генератора импульсов периодических задач...")
    
    # ПУБЛИЧНЫЕ РЫНОЧНЫЕ ДАННЫЕ (Обновляются по расписанию):
    asyncio.create_task(quotes_clock_loop(db_instance))
    asyncio.create_task(rates_clock_loop(db_instance))
    asyncio.create_task(fundamentals_clock_loop(db_instance))
    
    # РЕАКТИВНЫЙ КОНВЕЙЕР: Запускаем параллельную фоновую обработку нашей асинхронной очереди
    asyncio.create_task(etf_queue_worker_loop(db_instance))
    
    # ПРИВАТНЫЕ ДАННЫЕ ПОРТФЕЛЯ: Сюда задачи не добавлять!
    """
    АРХИТЕКТУРНАЯ ЗАМЕТКА О СИНХРОНИЗАЦИИ ПОРТФЕЛЯ (sync_by_account_number):
    Контур синхронизации личных аккаунтов (кэш, состав портфеля, ордера) НАМЕРЕННОНЕ ВКЛЮЧЕН в этот файл расписания.
    Синхронизация данных пользователя работает по событийно-ориентированной схеме:
    1. В реальном времени — через демона вебсокетов (fb_websocket_daemon.py) при каждом чихе по API.
    2. По запросу — напрямую из Telegram-бота (uport_ai_bot.py) при нажатии кнопки обновления.
    Дублировать эти вызовы по Cron-таймеру здесь не нужно во избежание лишней нагрузки.
    """