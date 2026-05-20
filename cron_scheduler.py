import asyncio
from datetime import datetime

# Напрямую импортируем наши изолированные рабочие утилиты
from site_connectors.sync_rates_yhoo import sync_rates
from brokers_connectors.sync_quotes_fb import sync_quotes_fb_branch
from brokers_connectors.sync_quotes_t212 import sync_quotes_t212_branch
from brokers_connectors.fb_client import FreedomBrokerClient

# НАСТРОЙКИ ПЕРИОДИЧНОСТИ (в секундах). Бот сможет менять их на лету.
QUOTES_SYNC_INTERVAL = 900  # 15 минут для акций
RATES_SYNC_INTERVAL = 7200  # 2 часа для макрокурсов валют Форекс


# === НАПРЯМЫЕ ФУНКЦИИ ОБНОВЛЕНИЯ (Вызываются и по кнопке, и по Cron) ===

def run_quotes_update(db_instance):
    """Сбор тикеров из базы и точечный вызов REST-коннекторов брокеров."""
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] Прямой запуск обновления котировок акций...")
    
    # Динамически опрашиваем СУБД — получаем список брокеров
    all_brokers = db_instance.execute_query("SELECT id, name FROM public.brokers;")
    if not all_brokers:
        print("⚠️ [Cron]: Справочник brokers пуст.")
        return

    # Динамический цикл по брокерам
    for broker in all_brokers:
        b_id = int(broker['id'])
        b_name = broker['name']
        
        # Для текущего брокера вытаскиваем из СУБД только его тикеры и множители
        sql_tickers = f"""
            SELECT t.id, t.full_ticker, c.multiplier 
            FROM public.tickers t
            JOIN public.currencies c ON t.currency_id = c.id
            WHERE t.broker_id = {b_id};
        """
        tickers_data = db_instance.execute_query(sql_tickers)
        if not tickers_data:
            continue
            
        print(f"💼 [Cron]: Обновляю {len(tickers_data)} бумаг у брокера {b_name} (ID: {b_id})")
        
        if b_id == 1:
            sync_quotes_fb_branch(tickers_data, db_instance, FreedomBrokerClient)
        elif b_id == 2:
            sync_quotes_t212_branch(tickers_data, db_instance)

def run_rates_update(db_instance):
    """Прямой вызов сбора Форекс-курсов через Yahoo Finance."""
    print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] Прямой запуск обновления курсов валют...")
    sync_rates(db_instance)


# === ПЕТЛИ ВРЕМЕНИ (РЕЛЕ) ===

async def quotes_clock_loop(db_instance):
    """Реле времени цен акций (раз в 15 минут)"""
    print(f"⏱️ [Реле Времени]: Контур цен акций успешно взведен.")
    while True:
        try:
            await asyncio.to_thread(run_quotes_update, db_instance)
        except Exception as e:
            print(f"❌ [Cron Цен Error]: {e}")
        await asyncio.sleep(QUOTES_SYNC_INTERVAL)

async def rates_clock_loop(db_instance):
    """Реле времени макрокурсов валют (раз в 2 часа)"""
    print(f"⏱️ [Реле Времени]: Контур курсов валют успешно взведен.")
    while True:
        try:
            await asyncio.to_thread(run_rates_update, db_instance)
        except Exception as e:
            print(f"❌ [Cron Валют Error]: {e}")
        await asyncio.sleep(RATES_SYNC_INTERVAL)

async def start_clocks(db_instance):
    """Точка старта реле времени фоновых периодических задач проекта UPort"""
    print("🧠 [Реле Времени]: Запуск генератора импульсов периодических задач...")
    
    # ПУБЛИЧНЫЕ РЫНОЧНЫЕ ДАННЫЕ (Обновляются по расписанию):
    asyncio.create_task(quotes_clock_loop(db_instance))
    asyncio.create_task(rates_clock_loop(db_instance))

    # ПРИВАТНЫЕ ДАННЫЕ ПОРТФЕЛЯ: Сюда задачи не добавлять! 
    """
    АРХИТЕКТУРНАЯ ЗАМЕТКА О СИНХРОНИЗАЦИИ ПОРТФЕЛЯ (sync_by_account_number):
    Контур синхронизации личных аккаунтов (кэш, состав портфеля, ордера) НАМЕРЕННО 
    НЕ ВКЛЮЧЕН в этот файл расписания. 
    Синхронизация данных пользователя работает по событийно-ориентированной схеме:
    1. В реальном времени — через демона вебсокетов (fb_websocket_daemon.py) при каждом чихе по API.
    2. По запросу — напрямую из Telegram-бота (uport_ai_bot.py) при нажатии кнопки обновления.
    Дублировать эти вызовы по Cron-таймеру здесь не нужно во избежание лишней нагрузки.
    """