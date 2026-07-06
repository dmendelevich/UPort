# ──────────────────────────────────────────────────────────────────────────────────────────
# ⏰ РАСПИСАНИЕ НОЧНЫХ КОНТУРОВ ФОНОВОЙ АВТОМАНИЗАЦИИ И СИНХРОНИЗАЦИИ ЭКОСИСТЕМЫ UPort
# ──────────────────────────────────────────────────────────────────────────────────────────
# Время  | Задача конвейера                 | Исполнитель (Модуль ядра системы)
# ───────┼──────────────────────────────────┼───────────────────────────────────────────────
# 01:00  | Ступень 1: Пылесос и Инжектор    | site_connectors/tv_market_injector.py
#        | (Сбор Strong Buy и Падших Ангел) | 
# ───────┼──────────────────────────────────┼───────────────────────────────────────────────
# 03:00  | Ступень 2: Догрузка Уолл-стрит   | site_connectors/sync_fundamentals_yhoo.py
#        | (CAGR, RSI, Target Prices Yahoo) |
# ───────┼──────────────────────────────────┼───────────────────────────────────────────────
# 04:00  | Альфа-Сито: Математический отсев | analytics/portfolio_auditor.py (Новый воркер)
#        | (Сортировка 9999 в вотчлисты П10)|
# ───────┼──────────────────────────────────┼───────────────────────────────────────────────
# 05:00  | Ступень 3: Суточный Дворник СУБД | cron_scheduler.py ➡️ run_database_janitor
#        | (Очистка логов и старого мусора) |
# ───────┼──────────────────────────────────┼───────────────────────────────────────────────
# 05:05  | Ступень 3: Боевой ИИ-Управляющий | analytics/ai_strategist.py
#        | (Аудит кошельков, алерты VseGPT) |
# ──────────────────────────────────────────────────────────────────────────────────────────

# Добавить:
# Мы очищаем базу не по капризному флагу deleted от брокера, а по факту неактивности и древности записи [单元].На уровне SQL-команды хрон ночью выполняет ровно один зрячий запрос:sqlDELETE FROM public.alerts 
# WHERE is_active = false 
#   AND updated_at < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - INTERVAL '1 day';

# Добавить декомпозицию фондов по воскресеньям

import asyncio
import logging
from datetime import datetime, timezone

# Напрямую импортируем наши изолированные рабочие утилиты
from site_connectors.sync_rates_yhoo import sync_rates
from site_connectors.sync_fundamentals_yhoo import sync_fundamentals

# 🔥 ВМЕСТО СТАРЫХ КЛИЕНТОВ И ВОРКЕРОВ FB ИМПОРТИРУЕМ НОВЫЙ АВТОНОМНЫЙ ОБНОВИТЕЛЬ
from brokers_connectors.sync_quotes_fb import sync_quotes_fb_autonomous
from brokers_connectors.sync_quotes_t212 import sync_quotes_t212_branch

# 🔥 ИМПОРТИРУЕМ НАШ НОВЫЙ БОЕВОЙ ВОРКЕР СИНХРОНИЗАЦИИ АЛЕРТОВ ФБ
from brokers_connectors.sync_alerts_fb import sync_all_broker_alerts

# Импортируем нашу глобальную асинхронную очередь задач из ядра
from database import ETF_LOOK_THROUGH_QUEUE


# ─────────────────────────────────────────────────────────
# === ГЛОБАЛЬНЫЕ НАСТРОЙКИ ПЕРИОДИЧНОСТИ (В СЕКУНДАХ) ===
# ─────────────────────────────────────────────────────────
QUOTES_SYNC_INTERVAL = 900          # 15 минут для котировок акций
RATES_SYNC_INTERVAL = 7200          # 2 часа для макрокурсов валют Форекс
FUNDAMENTALS_CHECK_INTERVAL = 3600  # 1 час для проверки ночного реле фундаментала
ETF_WORKER_PAUSE = 5                # Дыхательная пауза воркера между разбором фондов (секунд)

# 🔥 СТАНДАРТ v3.0: ДИНАМИЧЕСКИЙ ТАЙМЛАЙН СИНХРОНИЗАЦИИ С РЫНКОМ США (UTC)
# Биржи NYSE/NASDAQ закрываются в 16:00 по Нью-Йорку (летом это 20:00 UTC, зимой - 21:00 UTC).
# Задаем константу 21 с часом запаса. В Москве на часах в этот момент будет ровно 00:00 (полночь).
USA_MARKET_CLOSE_HOUR_UTC = 21

# Переводим целевые часы из абсолютных в ОТНОСИТЕЛЬНЫЕ с защитным оператором % 24
# Запуск фундаментала (через 3 часа после закрытия): летом в 00:00 UTC (03:00 ночи по Москве)
FUNDAMENTALS_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 3) % 24        

# 🔥 НОВЫЕ БОЕВЫЕ ПАРАМЕТРЫ КОНТУРА АЛЕРТОВ И ДВОРНИКА СУБД v1.0
ALERTS_SYNC_INTERVAL = 300          # 5 минут для планового сбора алертов Freedom Broker
JANITOR_CHECK_INTERVAL = 3600       # 1 час для проверки будильника дворника СУБД

# Запуск Дворника СУБД (через 5 часов после закрытия): летом в 02:00 UTC (05:00 утра по Москве)
JANITOR_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 5) % 24             

# === НАПРЯМЫЕ ФУНКЦИИ ОБНОВЛЕНИЯ И ОЧИСТКИ ===

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
        
        # Обработка Freedom Broker (ID: 1) на новых автономных рельсах
        if b_id == 1:
            logging.info(f"💼 [Cron]: Запуск автономного обновления котировок для брокера {b_name} (ID: 1)")
            sync_quotes_fb_autonomous()
            
        # Обработка Trading 212 (ID: 2) по старой схеме до ее модернизации
        elif b_id == 2:
            sql_tickers = f"""
                SELECT DISTINCT ON (l.broker_symbol) l.id AS listing_id, l.broker_symbol AS full_ticker, c.multiplier 
                FROM public.watchlist w
                JOIN public.listings l ON w.listing_id = l.id
                JOIN public.portfolios p ON w.portfolio_id = p.id
                JOIN public.currencies c ON l.currency_id = c.id
                WHERE p.broker_id = {b_id} AND (w.considered_at IS NOT NULL OR w.watched_at IS NOT NULL OR w.ordered_at IS NOT NULL OR w.bought_at IS NOT NULL);
            """
            tickers_data = db_instance.execute_query(sql_tickers)
            if not tickers_data:
                continue
                
            logging.info(f"💼 [Cron]: Обновляю {len(tickers_data)} активных листингов у брокера {b_name} (ID: {b_id})")
            sync_quotes_t212_branch(tickers_data, db_instance)

def run_rates_update(db_instance):
    """Прямой вызов сбора Форекс-курсов через Yahoo Finance."""
    logging.info("⚡ Прямой запуск обновления курсов валют...")
    sync_rates(db_instance)

def run_database_janitor(db_instance):
    """🔥 ДВОРНИК СУБД: Выполняет плановое удаление протухшего мусора из таблиц по ТЗ."""
    logging.info("🧹 [ДВОРНИК]: Старт суточной очистки и стерилизации базы данных UPort...")
    
    # Правило 2: Очистка сработавших/неактивных алертов, лежащих без дела более 7 дней
    sql_clean_alerts = """
        DELETE FROM public.alerts 
        WHERE is_active = false 
          AND triggered_at < CURRENT_TIMESTAMP - INTERVAL '7 days';
    """
    # Правило 1: Полное удаление распроданных позиций из watchlist по истечении 30 дней
    sql_clean_watchlist = """
        DELETE FROM public.watchlist 
        WHERE sold_out_at IS NOT NULL 
          AND sold_out_at < CURRENT_TIMESTAMP - INTERVAL '30 days';
    """
    
    try:
        db_instance.execute_query(sql_clean_alerts)
        db_instance.execute_query(sql_clean_watchlist)
        logging.info("🧹 [ДВОРНИК УСПЕХ]: Стерилизация завершена. Протухшие алерты (>7д) и закрытые вотчлисты (>30д) стёрты.")
    except Exception as janitor_err:
        logging.error(f"❌ [ДВОРНИК КРИТИЧЕСКИЙ СБОЙ]: Ошибка очистки СУБД: {janitor_err}")


# === ПЕТЛИ ВРЕМЕНИ (РЕЛЕ БОЕВЫХ ЦИКЛОВ) ===

async def alerts_clock_loop(db_instance):
    """🔥 РЕЛЕ АЛЕРТОВ ФБ: Непрерывный опрос брокера раз в 5 минут."""
    logging.info(f"⏱️ [Реле Времени]: Контур планового сбора алертов Freedom Broker взведен (Раз в {ALERTS_SYNC_INTERVAL}с).")
    while True:
        try:
            # Вызываем наш боевой синхронизатор в изолированном асинхронном потоке thread
            await asyncio.to_thread(sync_all_broker_alerts)
        except Exception as e:
            logging.error(f"❌ [Cron Алертов Error]: {e}")
        await asyncio.sleep(ALERTS_SYNC_INTERVAL)

async def janitor_clock_loop(db_instance):
    """🔥 УМНЫЙ СУТОЧНЫЙ БУДИЛЬНИК ДВОРНИКА: Просыпается раз в час и ждет 5:00 утра."""
    logging.info(f"⏱️ [Реле Времени]: Контур суточного Дворника СУБД взведен на дежурство (Цель: {JANITOR_TARGET_HOUR:02d}:00 утра).")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == JANITOR_TARGET_HOUR:
                logging.info(f"🧹 [Cron]: На часах {JANITOR_TARGET_HOUR:02d}:00 утра. Запуск планового Дворника...")
                await asyncio.to_thread(run_database_janitor, db_instance)
        except Exception as e:
            logging.error(f"❌ [Janitor Clock Error]: {e}")
            
        await asyncio.sleep(JANITOR_CHECK_INTERVAL)

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
    """УМНОЕ НОЧНОЕ РЕЛЕ ВРЕМЕНИ: Просыпается раз в час и ждет 3:00 ночи."""
    logging.info("⏱️ [Реле Времени]: Контур фундаментального анализа акций УОЛЛ-СТРИТ взведен.")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == FUNDAMENTALS_TARGET_HOUR:
                logging.info(f"🌙 [Cron]: На часах {FUNDAMENTALS_TARGET_HOUR:02d}:00 ночи. Запуск планового анализа...")
                await asyncio.to_thread(sync_fundamentals, db_instance)
        except Exception as e:
            logging.error(f"❌ [Cron Фундаментал Error]: {e}")
            
        await asyncio.sleep(FUNDAMENTALS_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────────────
# === ОБНОВЛЕННЫЙ ОДНОПОТОЧНЫЙ ВОРКЕР ОЧЕРЕДИ ETF LOOK-THROUGH (UPort Look-Through v4.0) ===
# ─────────────────────────────────────────────────────────────────────────────────────
async def etf_queue_worker_loop(db_instance):
    """
    Линейный реактивный конвейер UPort: Look-Through декомпозиция структуры фондов (ETF).
    ИСПРАВЛЕНО: Интегрирован защитный фильтр карантина ватчлиста и автоматическое 
    каноническое вживление живого ID родительского фонда в jsonb-карту provenance!
    """
    logging.info("⏱️ [Реле Времени]: Однопоточный воркер очереди ETF Look-Through взведен на дежурство.")
    import yfinance as yf
    import pandas as pd
    import json
    
    while True:
        task = await ETF_LOOK_THROUGH_QUEUE.get()
        t_id = task["id"]             # Уникальный числовой ID родительского фонда в public.tickers
        symbol = task["symbol"]       # Тикер фонда (например, COPX)
        full_ticker = task["full_ticker"]
        currency_id = task["currency_id"]
        broker_id = task["broker_id"]
        
        current_worker_pause = 1.0
        timestamp_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # ─── ШАГ 1: ЗАЩИТНЫЙ ФИЛЬТР КАРАНТИНА (СНАЙПЕРСКИЙ РЕНТГЕН ВОРКЕРА) ───
            # Проверяем, одобрен ли данный фонд Боссом или Папой в реальном семейном watchlist
            sql_check_wl = f"SELECT provenance FROM public.tickers WHERE id = {t_id};"
            db_res = await asyncio.to_thread(db_instance.execute_query, sql_check_wl)
            
            # Извлекаем словарь provenance из полученной строки СУБД
            provenance_map = {}
            if db_res and isinstance(db_res, list) and len(db_res) > 0:
                provenance_map = db_res[0].get("provenance", {}) if "provenance" in db_res[0] else {}
                
            # Ищем, горит ли в бортовом журнале фонда маркер реального листинга LST_ID=...
            has_wl_marker = any(str(k).startswith("LST_ID=") for k in provenance_map.keys())
            
            if not has_wl_marker:
                # 🛡️ ФОНД НА КАРАНТИНЕ: Лениво сбрасываем задачу без единого паразитного клика в сеть!
                ETF_LOOK_THROUGH_QUEUE.task_done()
                await asyncio.sleep(0.05)
                continue
                
            # ─── ШАГ 2: СБОР СОСТАВА ИЗ YAHOO FINANCE ДЛЯ ОДОБРЕННОГО ФОНДА ───
            yf_symbol = symbol.replace('.', '-') if symbol else symbol
            ticker_obj = yf.Ticker(yf_symbol)
            info = await asyncio.to_thread(lambda: ticker_obj.info)
            
            q_type = info.get('quoteType', 'Unknown') if info else 'Unknown'
            is_etf = (q_type == 'ETF' or (info and info.get('expenseRatio') is not None))
            
            if not is_etf:
                current_worker_pause = 1.0
            else:
                logging.info(f"🧱 [Очередь ETF]: Фонд {full_ticker} (ID={t_id}) прошел карантин! Начинаю сквозной разбор...")
                current_worker_pause = float(ETF_WORKER_PAUSE)
                
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
                        comp_sym = comp.get('Symbol') or comp.get('symbol') or comp.get('index') or comp.get('Ticker')
                        if not comp_sym:
                            continue
                            
                        raw_weight = comp.get('Holding Percent') or comp.get('holdingPercent') or comp.get('weight') or comp.get('Value') or 0
                        weight = float(raw_weight)
                        if 0 < weight < 1.0: 
                            weight = weight * 100
                        
                        if weight == 0:
                            continue
                            
                        comp_global_symbol = str(comp_sym).strip().upper()
                        
                        # Легализуем акцию-компонент через ядро UPort
                        comp_id, comp_listing_id = await asyncio.to_thread(
                            db_instance.ensure_ticker_v2,
                            broker_id=broker_id,
                            broker_symbol=comp_global_symbol,
                            fallback_currency=currency_id,
                            fb_client=None
                        )
                        
                        if not comp_id:
                            continue

                        # ─── ШАГ 3: ЮВЕЛИРНОЕ ДЕЛЬТА-ВЖИВЛЕНИЕ ЖИВОГО ID РОДИТЕЛЬСКОГО ФОНДА ───
                        # Функция jsonb_set(..., false) гарантирует сохранение даты первичного залета бумаги!
                        sql_update_provenance = f"""
                            UPDATE public.tickers
                            SET provenance = jsonb_set(
                                provenance, 
                                '{{ETF_LT_ID={t_id}}}', 
                                '"{timestamp_str}"'::jsonb, 
                                false
                            )
                            WHERE id = {comp_id};
                        """
                        await asyncio.to_thread(db_instance.execute_query, sql_update_provenance)

                        # Сохраняем вес компонента в таблицу декомпозиции весов
                        sql_save_component = f"""
                            INSERT INTO public.etf_holdings (etf_ticker_id, component_ticker_id, weight_percentage, last_updated_at)
                            VALUES ({t_id}, {comp_id}, {weight:.2f}, CURRENT_TIMESTAMP)
                            ON CONFLICT (etf_ticker_id, component_ticker_id) 
                            DO UPDATE SET weight_percentage = EXCLUDED.weight_percentage, last_updated_at = CURRENT_TIMESTAMP;
                        """
                        await asyncio.to_thread(db_instance.execute_query, sql_save_component)
                        
                    logging.info(f"🧱 [Очередь ETF]: Сквозной разбор фонда {full_ticker} успешно завершен. Сохранено {len(holdings_data)} компонентов.")
                else:
                    logging.warning(f"🧱 [Очередь ETF]: Не удалось извлечь внутренности для {full_ticker}.")

        except Exception as err:
            logging.error(f"❌ [Очередь ETF CRITICAL ERROR]: Ошибка воркера на тикере {full_ticker}: {err}")
            current_worker_pause = 2.0
        finally:
            ETF_LOOK_THROUGH_QUEUE.task_done()
            await asyncio.sleep(current_worker_pause)

# ─────────────────────────────────────────────────────────
# === ГЛАВНАЯ ТОЧКА СБОРКИ ВСЕХ ПЕТЕЛЬ ВРЕМЕНИ CRON ===
# ─────────────────────────────────────────────────────────
async def start_clocks(db_instance):
    """Оркестратор планировщика UPort: собирает все циклы в единую асинхронную группу."""
    logging.info("🚀 [Планировщик UPort]: Запуск всех контуров реле времени фоновых задач...")
    
    # Первичный принудительный синхрон котировок, курсов и алертов при старте ОС сервера
    try:
        await asyncio.to_thread(run_quotes_update, db_instance)
        await asyncio.to_thread(run_rates_update, db_instance)
        await asyncio.to_thread(sync_all_broker_alerts)  # Первичный сбор алертов при старте системы
    except Exception as boot_err:
        logging.error(f"⚠️ Ошибка первичной инициализации данных при загрузке Cron: {boot_err}")

    # Запускаем бесконечные асинхронные петли в Event Loop
    await asyncio.gather(
        quotes_clock_loop(db_instance),
        rates_clock_loop(db_instance),
        fundamentals_clock_loop(db_instance),
        alerts_clock_loop(db_instance),    # 5-минутный контур алертов Freedom Broker
        janitor_clock_loop(db_instance),   # Суточный контур Дворника СУБД на 5:00 утра
        etf_queue_worker_loop(db_instance) # Конвейер Look-Through разбора фондов
    )
