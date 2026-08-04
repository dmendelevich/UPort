#!/usr/bin/env python3
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

# Подтягиваем системные пути ядра UPort, чтобы Python всегда видел модуль database
sys.path.append(str(Path(__file__).parent.parent.resolve()))

from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient
from analytics.ladder_step_watcher import check_ladder_step_triggers
from analytics.price_move_watcher import check_price_moves

def sync_quotes_fb_autonomous():
    """
    Автономный пакетный REST-обновитель цен акций Freedom Broker Казахстан.
    Самостоятельно собирает тикеры из СУБД (из JSON-карты), запрашивает API
    и обновляет last_price в public.listings по стандарту времени UPort.
    """
    logging.debug("📡 [REST FB]: Запуск автономной синхронизации котировок...")

    # 1. Извлекаем short_name брокера для динамического ключа в JSON
    sql_broker = "SELECT short_name FROM public.brokers WHERE id = 1 LIMIT 1;"
    broker_rows = db_sys.execute_query(sql_broker)
    
    if not broker_rows:
        logging.error("❌ [REST FB ERROR]: Брокер с ID=1 не найден в таблице public.brokers.")
        return
        
    broker_key = broker_rows[0]['short_name'] # Вернет 'FB'

    # 2. Схлопнутый SQL-запрос: достаем тикер прямо из JSON-карты по ключу брокера
    sql_get_tickers = f"""
        SELECT 
            l.id AS listing_id,
            (t.ticker_name_map ->> '{broker_key}') AS full_ticker
        FROM public.listings l
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE l.broker_id = 1 
          AND (t.ticker_name_map ->> '{broker_key}') IS NOT NULL;
    """
    tickers_data = db_sys.execute_query(sql_get_tickers)

    if not tickers_data:
        logging.warning(f"⚠️ [REST FB]: В СУБД не найдено активных листингов для брокера {broker_key}.")
        return

    # Динамически собираем список тикеров для отправки одной пачкой брокеру
    tickers_list = [row['full_ticker'] for row in tickers_data]
    # Полный список тикеров -- на DEBUG (Claude/BACKLOG.md №28): сырой Python-список из
    # ~150-200 тикеров в одну строку лога нечитаем даже для отладки, только счётчик на INFO.
    logging.info(f"📡 [REST FB]: Запрос котировок getStockQuotesJson для пачки из {len(tickers_list)} тикеров.")
    logging.debug(f"📡 [REST FB]: Полный список пачки: {tickers_list}")

    # 3. Сборка фабричного клиента по user_id=1 (для общих котировок)
    fb_client = FreedomBrokerClient.create_for_user(user_id=1, db_instance=db_sys)
    
    if not fb_client:
        logging.error("❌ [REST FB ERROR]: Не удалось собрать фабричный клиент брокера через класс.")
        return

    try:
        # Отправляем пакетный запрос строго по нашему проверенному шаблону execute
        raw_res = fb_client.execute(command="getStockQuotesJson", params={"tickers": tickers_list})
        
        if isinstance(raw_res, dict) and "error" in raw_res:
            logging.error(f"❌ [REST FB API ERROR]: {raw_res['error']}")
            return
            
        # Проваливаемся в result -> q согласно спецификации брокера
        result_node = raw_res.get("result", {}) if isinstance(raw_res, dict) else {}
        result_array = result_node.get("q", []) if isinstance(result_node, dict) else []
        
        if not result_array or not isinstance(result_array, list):
            logging.warning("⚠️ [REST FB]: Сервер брокера вернул пустой массив котировок.")
            return

        # Мапим очищенный full_ticker на listing_id, полученный напрямую из БД
        listing_id_map = {row['full_ticker']: int(row['listing_id']) for row in tickers_data}

        updated_count = 0
        bad_price_count = 0

        for quote in result_array:
            ticker = quote.get("c")
            if not ticker or ticker not in listing_id_map:
                continue

            raw_price = quote.get("ltp")

            # 🔥 ЖЕЛЕЗНЫЙ ЗАГРАДИТЕЛЬНЫЙ БАРЬЕР ОТ НУЛЕВЫХ И НАНО-ЦЕН БРОКЕРА
            if raw_price is not None and str(raw_price) != 'nan' and float(raw_price) > 0.0:
                final_price = float(raw_price)
                l_id = listing_id_map[ticker]

                # 🔥 СТАНДАРТ ВРЕМЕНИ UPORT — СТЕРИЛЬНЫЙ UTC ДО СЕКУНД TIMESTAMP(0)
                sql_update_listing = f"""
                    UPDATE public.listings
                    SET last_price = {final_price},
                        last_updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                    WHERE id = {l_id};
                """
                db_sys.execute_query(sql_update_listing)
                updated_count += 1
            else:
                bad_price_count += 1
                logging.warning(f"   • {ticker:8} ⚠️ В пакете брокера отсутствует или некорректно значение ltp: {raw_price}")

        if bad_price_count > 0:
            logging.warning(f"📡 [REST FB]: Цикл завершён с ошибками -- обновлено {updated_count}, без цены {bad_price_count} из {len(tickers_list)}.")
        else:
            logging.debug(f"📡 [REST FB]: Цикл завершён -- обновлено {updated_count} из {len(tickers_list)}.")

    except Exception as e:
        logging.error(f"❌ [REST FB CRITICAL ERROR]: Сбой пакетного апдейта котировок: {e}")

    # Протухание приказов/алертов ушло из пуш-уведомлений цикла котировок в дайджест
    # (analytics/order_alert_staleness.py, п.5 БЭКЛОГА, 2026-07-30) -- "медленное" событие,
    # не требует немедленной реакции, пересчитывается заново при каждой сборке дайджеста.
    # Готовность следующего шага лесенки остаётся здесь -- рыночно-зависимая проверка
    # (см. Claude/09_pipeline_reconciliation.md). Сбой не должен ронять сам синк котировок.
    try:
        check_ladder_step_triggers(db_sys)
    except Exception as ladder_err:
        logging.error(f"⚠️ [REST FB]: Сбой проверки готовности следующего шага лесенки: {ladder_err}")

    # PriceMoveWatcher (см. Claude/05_strategy_screen_and_kubiki.md): резкое движение цены
    # за окно времени -- та же причина вызова здесь, а не в дайджесте (рыночно-зависимо)
    try:
        check_price_moves(db_sys)
    except Exception as price_move_err:
        logging.error(f"⚠️ [REST FB]: Сбой проверки резких движений цены (PriceMoveWatcher): {price_move_err}")

if __name__ == "__main__":
    # Настройка базового логирования для возможности прямого автономного запуска файла
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sync_quotes_fb_autonomous()
