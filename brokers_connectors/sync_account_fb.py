import os
import json
from dotenv import load_dotenv
from pathlib import Path

import traceback
import logging

from brokers_connectors.sync_strategy_asset_fb import SyncStrategyAssetFB   #!!!!!!!!!!!!!!!!!!!

# Загружаем переменные из .env
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

class FreedomBrokerSyncManager:
    """Менеджер для глубокой синхронизации активов и мультивалютных счетов Freedom Broker."""

    def __init__(self, db_instance, fb_client_class):
        self.db = db_instance
        self.fb_client_class = fb_client_class

    def sync_by_account_number(self, account_number: str) -> dict:
        """Синхронизирует активы, кэш и ордера, динамически создавая структуру в БД при её отсутствии."""

        # Инициализируем наш новый менеджер стратегий
        strat_sync = SyncStrategyAssetFB(self.db)   #!!!!!!!!!!!!!!!!!!!

        try:
            w_check = None  # 🔥 Объявляем переменную пустой на старте функции!

            # 1. Проверяем наличие счета в таблице accounts
            acc_sql = """
                SELECT a.user_id, a.portfolio_id, a.account_type, u.name as owner_name, u.prefix
                FROM accounts a
                JOIN users u ON a.user_id = u.id
                WHERE a.account_number = %s LIMIT 1
            """
            acc_data = self.db.execute_query(acc_sql, (account_number,))

            # АВТОМАТИЧЕСКОЕ СОЗДАНИЕ: Если таблица accounts пуста, генерируем базовую структуру
            if not acc_data or len(acc_data) == 0:
                logging.info(f"♻️ [Sync Manager]: Счет {account_number} не найден в accounts. Автоматическое создание...")

                is_deposit = account_number.startswith("D")
                look_num = account_number[1:] if is_deposit else account_number
                account_type = "deposit" if is_deposit else "trade"

                user_sql = "SELECT id, name, prefix, id as user_id FROM users WHERE account_number = %s LIMIT 1"
                user_rows = self.db.execute_query(user_sql, (look_num,))

                if not user_rows or len(user_rows) == 0:
                    raise ValueError(f"Критическая ошибка: Базовый аккаунт {look_num} не привязан ни к одному пользователю.")

                user_id = user_rows[0]['id']
                broker_id = 1 # Freedom Broker Казахстан всегда имеет ID = 1

                portfolio_id = None
                if account_type == "trade":
                    # Фильтр по broker_id обязателен: у владельца может быть больше одного портфеля
                    # (например, бумажный портфель с broker_id=NULL) -- без фильтра LIMIT 1 без ORDER BY
                    # мог бы непредсказуемо приписать реальные деньги брокера чужому портфелю.
                    p_sql = "SELECT id FROM portfolios WHERE owner_id = %s AND broker_id = %s ORDER BY id LIMIT 1"
                    p_rows = self.db.execute_query(p_sql, (user_id, broker_id))
                    if p_rows and len(p_rows) > 0:
                        portfolio_id = p_rows[0]['id']
                    else:
                        p_insert = "INSERT INTO portfolios (owner_id, name, broker_id) VALUES (%s, 'Основной', %s)"
                        self.db.execute_query(p_insert, (user_id, broker_id))
                        p_rows_retry = self.db.execute_query(p_sql, (user_id, broker_id))
                        portfolio_id = p_rows_retry[0]['id'] if p_rows_retry else None

                # Вызываем "Китов" для создания базовой USD строки кошелька
                self.db.ensure_currency("USD")
                self.db.ensure_account_sub_row(user_id, portfolio_id, broker_id, account_number, account_type, "USD")

                acc_data = self.db.execute_query(acc_sql, (account_number,))

            user_id = acc_data[0]['user_id']
            portfolio_id = acc_data[0]['portfolio_id']
            account_type = acc_data[0]['account_type']
            owner_name = acc_data[0]['owner_name']
            prefix = acc_data[0]['prefix']
            broker_id = 1

            if account_type == "deposit":
                api_key = os.getenv(f"FB_{prefix}_D_API_KEY")
                api_secret = os.getenv(f"FB_{prefix}_D_API_SECRET")
                logging_mode = "НАКОПИТЕЛЬНЫЙ (D-ключи)"
            else:
                api_key = os.getenv(f"FB_{prefix}_API_KEY")
                api_secret = os.getenv(f"FB_{prefix}_API_SECRET")
                logging_mode = "ТОРГОВЫЙ (Т-ключи)"

            fb_client = self.fb_client_class(public_key=api_key, private_key=api_secret)

            # Запрашиваем живые данные баланса и позиций по API
            raw_res = fb_client.execute("getPositionJson", params={})
            if isinstance(raw_res, dict) and "error" in raw_res:
                raise RuntimeError(f"Ошибка API Freedom Broker: {raw_res['error']}")
                
            result_node = raw_res.get("result", {})
            ps_node = result_node.get("ps", {})
            
            positions = ps_node.get("pos", [])
            cash_balances = ps_node.get("acc", [])

            # --- ЭТАП А: ОБНОВЛЕНИЕ МУЛЬТИВАЛЮТНОГО КЭША (ЧЕРЕЗ КИТОВ) ---
            for cash in cash_balances:
                currency = cash.get("curr", "USD")
                available = float(cash.get("s", 0))
                reserved = abs(float(cash.get("t2_out", 0))) 

                # Кит №1 и №2: Гарантируем, что валюта и субсчет физически существуют в СУБД
                self.db.ensure_currency(currency)
                self.db.ensure_account_sub_row(user_id, portfolio_id, broker_id, account_number, account_type, currency)

                # Теперь спокойно делаем UPDATE балансов
                sql_wallet_update = "UPDATE accounts SET cash_available = %s, cash_reserved = %s, last_updated = transaction_timestamp() WHERE account_number = %s AND currency_id = %s"
                self.db.execute_query(sql_wallet_update, (available, reserved, account_number, currency))

            # --- ЭТАП Б: ОБНОВЛЕНИЕ ЦЕННЫХ БУМАГ (ЧЕРЕЗ КИТОВ v3.0) ---
            synced_assets_count = 0
            if account_type == "trade" and portfolio_id:
                active_listing_ids = []
                total_market_value = 0
                
                # ФИКСИРУЕМ ВРЕМЯ СТАРТА СЕССИИ СИНКА
                time_res = self.db.execute_query("SELECT CURRENT_TIMESTAMP as now")
                session_start_time = time_res[0]['now']

                for pos in positions:
                    full_ticker = pos.get("i")
                    quantity = float(pos.get("q", 0))
                    avg_price = float(pos.get("price_a", 0))
                    currency = pos.get("curr", "USD")
                    market_val = float(pos.get("market_value", 0))

                    if not full_ticker or quantity <= 0:
                        continue

                    total_market_value += market_val
                    
                    # 🔥 ОБНОВЛЕНО v3.0: Запускаем универсальные Главные Ворота СУБД
                    # Передаем роль LST и живой fb_client из рук Синк-Менеджера.
                    # Ядро мгновенно проверит кэш, защитит базу и вернет чистые реляционные ключи.
                    ticker_id, listing_id = self.db.ensure_ticker_v3(
                        ticker_name_raw=full_ticker, 
                        caller_role="LST", 
                        caller_id=None,
                        broker_id=1, 
                        fb_client=fb_client
                    )

                    active_listing_ids.append(listing_id)

                    # 🔥 MASTER-ЗЕРКАЛИРОВАНИЕ v4.7: Фиксируем пятифлаговый жизненный цикл ПЕРЕД любыми операциями с деньгами
                    # Проверяем, зафиксирована ли уже эта бумага в планах этого портфеля
                    w_check = self.db.execute_query(
                        "SELECT id FROM public.watchlist WHERE portfolio_id = %s AND listing_id = %s;",
                        (portfolio_id, listing_id)
                    )

                    if w_check and len(w_check) > 0:
                        # Если бумага уже была в радаре/фокусе/приказе — бережно дописываем дату реальной покупки через COALESCE
                        w_id = w_check[0]['id'] if isinstance(w_check, list) else w_check['id']
                        self.db.execute_query("""
                            UPDATE public.watchlist
                            SET bought_at = COALESCE(bought_at, CURRENT_TIMESTAMP),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (int(w_id),))
                    else:
                        # Если купили сразу в терминале "мимо блокнота" — легализуем в watchlist одной секундной пачкой!
                        self.db.execute_query("""
                            INSERT INTO public.watchlist (portfolio_id, listing_id, watched_at, bought_at)
                            VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                        """, (portfolio_id, listing_id))

                    # 🔎 СНАЙПЕРСКАЯ ПРОВЕРКА MASTER-ТАБЛИЦЫ (Вынесена на самый верх!)
                    w_check = self.db.execute_query(
                        "SELECT id FROM public.watchlist WHERE portfolio_id = %s AND listing_id = %s;",
                        (portfolio_id, listing_id)
                    )

                    # 💵 РАБОТАЕМ С БАЛАНСАМИ ТАБЛИЦЫ ASSETS
                    asset_search = "SELECT id FROM assets WHERE portfolio_id = %s AND listing_id = %s"
                    asset_res = self.db.execute_query(asset_search, (portfolio_id, listing_id))

                    # 🔥 НАЧАЛО ВНЕДРЕНИЯ (ТОЧКА 1): Замеряем, сколько акций БЫЛО в базе до обновления   #!!!!!!!!!!!!!!!!!!!
                    old_quantity = strat_sync.get_current_quantity(portfolio_id, listing_id)
                    # 🔥 КОНЕЦ ВНЕДРЕНИЯ (ТОЧКА 1)

                    if asset_res and len(asset_res) > 0:
                        # Узел А: Обычное обновление цены или докупка существующей позиции
                        # 🔥 ЖЕСТКИЙ ФИКС РАСПАКОВКИ СПИСКА СУБД: Сначала извлекаем словарь, а потом берем id
                        if isinstance(asset_res, list):
                            a_row = asset_res[0]
                        else:
                            a_row = asset_res
                            
                        a_id = int(a_row['id'])
                        sql_asset_update = "UPDATE assets SET quantity = %s, avg_price = %s, last_updated = %s WHERE id = %s"
                        self.db.execute_query(sql_asset_update, (quantity, avg_price, session_start_time, a_id))

                        # Безопасно извлекаем w_id из w_check для обновления даты покупки
                        w_id = None
                        if w_check:
                            if isinstance(w_check, list) and len(w_check) > 0:
                                w_id = w_check[0].get('id')
                            elif isinstance(w_check, dict):
                                w_id = w_check.get('id')

                        if w_id is not None:
                            self.db.execute_query(
                                "UPDATE public.watchlist SET bought_at = COALESCE(bought_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                                (int(w_id),)
                            )
                    else:
                        # Узел Б: Первичное добавление абсолютно новой бумаги (Инсерт)
                        w_id = None
                        if w_check:
                            if isinstance(w_check, list) and len(w_check) > 0:
                                w_id = w_check[0].get('id')
                            elif isinstance(w_check, dict):
                                w_id = w_check.get('id')

                        if w_id is not None:
                            self.db.execute_query(
                                "UPDATE public.watchlist SET bought_at = COALESCE(bought_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                                (int(w_id),)
                            )
                        else:
                            # Если бумаги не было вообще ни в одном списке — легализуем её с нуля пачкой
                            self.db.execute_query(
                                "INSERT INTO public.watchlist (portfolio_id, listing_id, watched_at, bought_at) VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);",
                                (portfolio_id, listing_id)
                            )

                        # Фиксируем холдинг-дни position_opened_at
                        sql_asset_insert = """
                            INSERT INTO assets (portfolio_id, listing_id, quantity, avg_price, last_updated, position_opened_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (portfolio_id, listing_id)
                            DO UPDATE SET quantity = EXCLUDED.quantity, avg_price = EXCLUDED.avg_price, last_updated = EXCLUDED.last_updated;
                        """
                        self.db.execute_query(sql_asset_insert, (portfolio_id, listing_id, quantity, avg_price, session_start_time, session_start_time))

                    # 🔥 НАЧАЛО ВНЕДРЕНИЯ (ТОЧКА 2): Базовый asset обновлен! Запускаем умное распределение по стратегиям   #!!!!!!!!!!!!!!!!!!!
                    # quantity — это то новое количество, которое только что прислал брокер
                    strat_sync.distribute_asset_delta(
                        portfolio_id=portfolio_id, 
                        listing_id=listing_id, 
                        ticker_id=ticker_id, 
                        old_qty=old_quantity, 
                        new_qty=float(quantity)
                    )
                    # 🔥 КОНЕЦ ВНЕДРЕНИЯ (ТОЧКА 2)

                # 🔥 ИНТЕЛЛЕКТУАЛЬНЫЙ МАССОВЫЙ ФИКС ФАНТОМОВ v5.0 (КРУГОВОРОТ В WATCHLIST)
                # Старый поштучный цикл со статусами полностью удален ради чистоты природы Master-таблицы.
                if active_listing_ids:
                    # NOT IN (%s) со списком не работает через JSON-параметризацию (список
                    # адаптируется psycopg2 в ARRAY[...], не в набор для IN) -- рабочий эквивалент
                    # для "не входит в список" через массив: NOT (x = ANY(%s)), проверено отдельно
                    # на шлюзе включая пустой список (Claude/BACKLOG.md №81).
                    active_ids_list = list(active_listing_ids)

                    # 1. ПАКЕТНЫЙ UPDATE MASTER-ТАБЛИЦЫ: Находим все акции этого портфеля, которые брокер НЕ прислал
                    # (то есть они проданы в ноль), и бережно фиксируем им секунду закрытия sold_out_at.
                    sql_watchlist_sold_out = """
                        UPDATE public.watchlist
                        SET sold_out_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE portfolio_id = %s
                        AND NOT (listing_id = ANY(%s))
                        AND bought_at IS NOT NULL;
                    """
                    self.db.execute_query(sql_watchlist_sold_out, (portfolio_id, active_ids_list))

                    # 2. СТЕРИЛЬНАЯ ОЧИСТКА КОНТУРА ЖИВЫХ ДЕНЕГ
                    sql_cleanup = """
                        DELETE FROM assets
                        WHERE portfolio_id = %s
                        AND NOT (listing_id = ANY(%s));
                    """
                    self.db.execute_query(sql_cleanup, (portfolio_id, active_ids_list))

                # Обновляем агрегированную долларовую стоимость активов счета в accounts
                self.db.execute_query(
                    "UPDATE accounts SET assets_value = %s WHERE account_number = %s AND currency_id = 'USD'",
                    (total_market_value, account_number)
                )
                synced_assets_count = len(active_listing_ids)

            # --- ЭТАП В: СИНХРОНИЗАЦИЯ ОРДЕРОВ (ЧЕРЕЗ КИТОВ ПЕРЕД ТРАНЗАКЦИЕЙ) ---
            try:
                # Вызываем метод только для торговых счетов (на D-счетах ордеров на акции нет)
                if portfolio_id and account_type == "trade":
                    active_orders_list = fb_client.get_active_orders()
                    
                    # ЕДИНЫЙ ПРОТОКОЛ: Прогоняем каждый ордер через Китов v3.0 ДО открытия транзакции
                    for order in active_orders_list:
                        self.db.ensure_currency(order['currency_id'])
                        self.db.ensure_account_sub_row(user_id, portfolio_id, broker_id, account_number, account_type, order['currency_id'])
                        
                        # 🔥 ОБНОВЛЕНО v3.0: Гарантируем регистрацию листинга ордера брокера через Главные Ворота
                        ord_ticker_id, ord_listing_id = self.db.ensure_ticker_v3(
                            ticker_name_raw=order['ticker'], 
                            caller_role="LST", 
                            caller_id=None,
                            broker_id=1, 
                            fb_client=fb_client
                        )
                        
                    # 🔥 MASTER-ФИКС v5.3: Проверяем наличие Master-строки в watchlist с жесткой распаковкой списка СУБД
                    w_ord_check = self.db.execute_query(
                        "SELECT id FROM public.watchlist WHERE portfolio_id = %s AND listing_id = %s;",
                        (portfolio_id, ord_listing_id)
                    )

                    # Безопасно извлекаем w_o_id, страхуясь от списков словарей
                    w_o_id = None
                    if w_ord_check:
                        if isinstance(w_ord_check, list) and len(w_ord_check) > 0:
                            w_o_id = w_ord_check[0].get('id')
                        elif isinstance(w_ord_check, dict):
                            w_o_id = w_ord_check.get('id')

                    if w_o_id is not None:
                        # Если бумага уже была в слежке/фокусе, просто дописываем дату выставления ордера ordered_at
                        self.db.execute_query("""
                            UPDATE public.watchlist
                            SET ordered_at = COALESCE(ordered_at, CURRENT_TIMESTAMP),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (int(w_o_id),))
                    else:
                        # Если вы выставили ордер в терминале "мимо блокнота" — легализуем в watchlist с фиксацией даты ордера!
                        self.db.execute_query("""
                            INSERT INTO public.watchlist (portfolio_id, listing_id, watched_at, ordered_at)
                            VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                        """, (portfolio_id, ord_listing_id))
                    
                    # Все сущности на месте. Вызываем чистую и размеренную вставку ордеров.
                    # Метод экземпляра (Claude/BACKLOG.md №81) -- раньше вызывался статикой
                    # Database.sync_portfolio_orders(...) в обход роли/токена self.db.
                    self.db.sync_portfolio_orders(
                        portfolio_id=portfolio_id,
                        account_number=account_number,
                        api_orders=active_orders_list
                    )

            except Exception as o_err:
                logging.warning(f"⚠️ [Sync Manager]: Не удалось обновить слепок приказов: {o_err}")

            return {
                "owner_name": owner_name,
                "account_number": account_number,
                "account_type": account_type,
                "synced_assets": synced_assets_count,
                "mode": logging_mode
            }

            # В самом конце вашего кода функции обычно стоит return. Он тоже внутри try.
            # Находим самый финал функции и дописываем блок перехвата:
            
        except Exception as crash_err:
            # print()-дубликат этой же ошибки убран (Claude/BACKLOG.md №28) -- logging.error
            # ниже уже несёт тип ошибки, сообщение и полный трейсбэк, второй раз печатать незачем.
            logging.error(f"🚨 [КРАШ ЯДРА SYNC_BY_ACCOUNT_NUMBER]: {crash_err}\n{traceback.format_exc()}")
            
            # Пробрасываем ошибку дальше, чтобы веб-сокет знал о падении
            raise crash_err