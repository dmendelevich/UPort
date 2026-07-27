import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import json
import logging

# Глобальная потокобезопасная очередь задач для сквозного анализа ETF
ETF_LOOK_THROUGH_QUEUE = asyncio.Queue()

# Загружаем переменные из .env
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

class Database:
    def __init__(self, role: str = "BOT"):
        """
        Универсальный класс взаимодействия со шлюзом UPort AI Gateway.
        """
        self.url = "http://localhost:3000/query"
        
        if role == "SYSTEM":
            self.token = os.getenv("UPORT_TOKEN_SYSTEM")
        elif role == "AI":
            self.token = os.getenv("UPORT_TOKEN_AI")
        elif role == "BOT":
            self.token = os.getenv("UPORT_TOKEN_BOT")
        else:
            raise ValueError(f"Неизвестная роль базы данных: {role}")
            
        if not self.token:
            raise RuntimeError(f"Критическая ошибка: Токен для роли {role} не найден в .env.")

    def execute_query(self, sql_query: str) -> list:
        """Отправка SQL-запроса на шлюз с заголовком авторизации текущей роли."""
        headers = {"X-Token": self.token}
        payload = {"query": sql_query}
        
        try:
            response = requests.post(self.url, json=payload, headers=headers, timeout=10)
            if response.status_code != 200:
                # 🔥 ВЫТАСКИВАЕМ РЕАЛЬНЫЙ КРИК БАЗЫ ДАННЫХ ИЗ ШЛЮЗА
                try:
                    err_detail = response.json().get('detail', response.text)
                except:
                    err_detail = response.text
                print(f"🚨 [ШЛЮЗ КРИТИЧЕСКИЙ СБОЙ СУБД]: Код {response.status_code} на запрос: {sql_query[:50]}... | Текст ошибки: {err_detail}")
                return []
            return response.json()
        except Exception as e:
            print(f"🚨 [ЯДРО ERROR]: Шлюз вернул исключение на запрос: {sql_query[:60]}... | Ошибка: {e}")
            return []

    def execute_row(self, sql_query: str) -> dict:
        """
        Выполняет SQL-запрос и гарантированно возвращает ОДНУ строку в виде словаря.
        Если база данных ничего не нашла, возвращает пустой словарь {}.
        Идеально подходит для точечных запросов (LIMIT 1) и избавляет от скобок.
        """
        res = self.execute_query(sql_query)
        if isinstance(res, list) and len(res) > 0:
            return res[0]
        return {}

    # === ТРИ КИТА ИНФРАСТРУКТУРЫ (ПОДГОТОВКА ДО ТРАНЗАКЦИИ) ===

    def ensure_currency(self, currency_id: str):
        """Кит №1: Гарантирует наличие валюты в справочнике currencies."""
        sql = f"""
        INSERT INTO public.currencies (id, sign, multiplier)
        VALUES ('{currency_id}', '{currency_id}', 1.0)
        ON CONFLICT (id) DO NOTHING;
        """
        self.execute_query(sql)

    def ensure_account_sub_row(self, user_id: int, portfolio_id: int, broker_id: int, account_number: str, account_type: str, currency_id: str):
        """Кит №2: Гарантирует наличие мультивалютной строки субсчета в таблице accounts."""
        p_id_val = portfolio_id if portfolio_id else "NULL"
        sql = f"""
        INSERT INTO public.accounts (user_id, portfolio_id, broker_id, account_number, account_type, currency_id, cash_available, cash_reserved, assets_value)
        VALUES ({user_id}, {p_id_val}, {broker_id}, '{account_number}', '{account_type}', '{currency_id}', 0, 0, 0)
        ON CONFLICT (account_number, currency_id) DO NOTHING;
        """
        self.execute_query(sql)

    def ensure_ticker_v2(self, broker_id: int, broker_symbol: str, fallback_currency: str = "USD", fb_client = None) -> tuple:
        """
        Кит №3 (Академический v3.7): Универсальный поисковик тикеров без обрезки точек.
        Записывает в tickers.symbol строго чистую мировую истину без суффиксов брокера.
        """
        broker_symbol_clean = broker_symbol.strip().upper()
        
        # 1. СНАЙПЕРСКАЯ ПРОВЕРКА КЭША СУБД (0 запросов в сеть)
        sql_check = f"SELECT id, ticker_id FROM public.listings WHERE broker_id = {broker_id} AND broker_symbol = '{broker_symbol_clean}';"
        cache_res = self.execute_query(sql_check)
        if cache_res and isinstance(cache_res, list) and len(cache_res) > 0:
            row = cache_res[0]
            if isinstance(row, dict) and row.get('id') and row.get('ticker_id'):
                return int(row['ticker_id']), int(row['id'])

        # 2. ИНИЦИАЛИЗАЦИЯ ДЕФОЛТОВ НА СЛУЧАЙ СБОЯ СЕТИ БРОКЕРА
        symbol = broker_symbol_clean
        isin = "UNKNOWN"
        comp_name = "Unknown Company"
        currency_id = fallback_currency.upper()

        # 3. СУП-ПЕРЕВОДЧИК ИМЕН (1 контролируемый запрос в сеть)
        if broker_id == 1 and fb_client is not None:
            try:
                print(f"📡 [Ядро СУП]: Новый инструмент! Запрашиваю спецификацию Freedom Broker для '{broker_symbol_clean}'...")
                sec_info = fb_client.get_security_info(broker_symbol_clean)
                if sec_info and isinstance(sec_info, dict) and "error" not in sec_info:
                    # 🔥 ИЗВЛЕКАЕМ МИРОВУЮ ИСТИНУ: забираем готовый глобальный тикер (напр. 'ANTO.L')
                    # Больше никакой Python-обрезки по первой точке!
                    fetched_symbol = sec_info.get('default_ticker') or sec_info.get('ticker') or sec_info.get('char_code')
                    if fetched_symbol:
                        symbol = str(fetched_symbol).strip().upper()
                    if sec_info.get('isin'):
                        isin = str(sec_info['isin']).strip().upper()
                    if sec_info.get('name'):
                        comp_name = str(sec_info['name']).strip().replace("'", "''")
                    if sec_info.get('currency'):
                        currency_id = str(sec_info['currency']).strip().upper()
            except Exception as sup_err:
                print(f"⚠️ [Ядро СУП WARNING]: Ошибка СУП FB: {sup_err}")

        # 4. СИНХРОНИЗАЦИЯ СТЕРИЛЬНОГО СПРАВОЧНИКА TICKERS (3NF)
        self.ensure_currency(currency_id)
        
        # Убраны legacy-колонки full_ticker, suffix, broker_id. Пишем только мировой symbol!
        sql_insert_ticker = f"""
            INSERT INTO public.tickers (symbol, company_name, isin)
            VALUES ('{symbol}', '{comp_name}', '{isin}')
            ON CONFLICT (symbol) 
            DO UPDATE SET isin = CASE WHEN public.tickers.isin = 'UNKNOWN' THEN EXCLUDED.isin ELSE public.tickers.isin END;
        """
        self.execute_query(sql_insert_ticker)
        
        # Надежно забираем сгенерированный ID
        t_get = self.execute_query(f"SELECT id FROM public.tickers WHERE symbol = '{symbol}';")
        t_row = t_get[0] if t_get and isinstance(t_get, list) and len(t_get) > 0 else {}
        ticker_id = t_row.get('id')

        if not ticker_id:
            raise RuntimeError(f"Критическая ошибка ядра: Не удалось сгенерировать ticker_id для {symbol}")

        # 5. СИНХРОНИЗАЦИЯ ТАБЛИЦЫ-ПЕРЕСЕЧЕНИЯ (listings)
        sql_insert_listing = f"""
            INSERT INTO public.listings (ticker_id, broker_id, broker_symbol, currency_id)
            VALUES ({ticker_id}, {broker_id}, '{broker_symbol_clean}', '{currency_id}')
            ON CONFLICT (broker_id, broker_symbol) DO NOTHING;
        """
        self.execute_query(sql_insert_listing)
        
        l_res = self.execute_query(f"SELECT id FROM public.listings WHERE broker_id = {broker_id} AND broker_symbol = '{broker_symbol_clean}';")
        l_row = l_res[0] if l_res and isinstance(l_res, list) and len(l_res) > 0 else {}
        listing_id = l_row.get('id')

        if not listing_id:
            raise RuntimeError(f"Критическая ошибка ядра: Не удалось сгенерировать listing_id для {broker_symbol_clean}")

        # 6. ПОСТАНОВКА В ОЧЕРЕДЬ ETF LOOK-THROUGH
        try:
            check_h = self.execute_query(f"SELECT 1 FROM public.etf_holdings WHERE etf_ticker_id = {ticker_id} LIMIT 1;")
            if not check_h:
                # Передаем чистый мировой символ компонента в очередь
                task_data = {"id": int(ticker_id), "symbol": symbol, "suffix": "US", "full_ticker": broker_symbol_clean, "currency_id": currency_id, "broker_id": broker_id}
                ETF_LOOK_THROUGH_QUEUE.put_nowait(task_data)
        except Exception as q_err:
            print(f"⚠️ Предупреждение постановки {symbol} в ETF очередь: {q_err}")

        return int(ticker_id), int(listing_id)

    def ensure_ticker_v3(self, ticker_name_raw: str, caller_role: str, caller_id=None, broker_id: int = None, fb_client = None) -> tuple:
        """
        ГЛАВНЫЕ ВОРОТА СУБД UPort v3.0 (УНИВЕРСАЛЬНЫЙ АВТОНОМНЫЙ ШЛЮЗ ПАСПОРТИЗАЦИИ).
        
        Параметры:
            ticker_name_raw (str): Сырой тикер от любого источника (напр. 'AAPL', 'BROS.US', 'BRK.B').
            caller_role (str): Роль вызывающего процесса ('TG_USR', 'LST', 'ETF_LT', 'MS').
            caller_id: Спецификация автора запроса (user_db_id, parent_etf_id, код индекса или None).
            broker_id (int): Идентификатор брокера из СУБД (1 - Freedom Broker, 2 - Trading212), если применимо.
            fb_client: Живой объект транспорта Freedom Broker (если передан сверху из Синк-Менеджера).
            
        Возвращает:
            tuple: (ticker_id, listing_id) — эталонные реляционные ключи СУБД.
        """
        from utils import ensure_ticker_passport_in_db, manage_provenance
        
        symbol_clean = str(ticker_name_raw).strip().upper()
        role_clean = str(caller_role).strip().upper()
        
        logging.info(f"🧱 [ЯДРО v3.0 START]: Входной запрос от [{role_clean}] для символа '{symbol_clean}'")
        
        ticker_id = None
        res_ticker = None

        # ─── ШАГ 1: ПЕРВИЧНАЯ ПРОВЕРКА НАЛИЧИЯ ТИКЕРА В ЛОКАЛЬНОМ СПРАВОЧНИКЕ ───
        json_pattern_fb = json.dumps({"FB": symbol_clean})
        json_pattern_t212 = json.dumps({"T212": symbol_clean})
        
        sql_check_ticker = f"""
            SELECT id FROM public.tickers 
            WHERE ticker_name_map @> '{json_pattern_fb}'::jsonb 
            OR ticker_name_map @> '{json_pattern_t212}'::jsonb 
            LIMIT 1;
        """
        res_ticker = self.execute_query(sql_check_ticker)
        if res_ticker and isinstance(res_ticker, list) and len(res_ticker) > 0:
            row_t = res_ticker[0] if isinstance(res_ticker, list) else res_ticker
            ticker_id = int(row_t.get('id') if isinstance(row_t, dict) else row_t)
            logging.info(f"🎯 [ЯДРО v3.0 КЭШ-Т] Справочник по матрице имен содержит '{symbol_clean}'. ticker_id = {ticker_id}")

        # ─── ШАГ 2: МИРОВАЯ ЛЕГАЛИЗАЦИЯ (ВЫЗОВ ПАСПОРТИСТКИ ДЛЯ РЕАЛЬНО НОВЫХ БУМАГ) ───
        # Если по кэшу ничего не нашли (или это торговый след брокера LST, где нужно дозаписать кодировки)
        if ticker_id is None:
            # 🔥 ИСПРАВЛЕНО v3.2: Переключаем внутренний контур ядра на монолитную фабрику связей по user_id=1
            # Это гарантирует успешную сборку робота в контексте ТГ-бота без явного вызова load_dotenv()
            if fb_client is None and broker_id == 1:
                try:
                    from brokers_connectors.fb_client import FreedomBrokerClient
                    fb_client = FreedomBrokerClient.create_for_user(user_id=1, db_instance=self)
                    if fb_client:
                        logging.info("📡 [ЯДРО v3.0]: Системный робот FreedomBrokerClient успешно собран через фабрику по user_id=1.")
                except Exception as transport_err:
                    logging.error(f"⚠️ [ЯДРО v3.0]: Не удалось собрать фабричного робота брокера: {transport_err}")

            # Вызываем Паспортистку. Теперь fb_client гарантированно живой для контура ФБ!
            ticker_id = ensure_ticker_passport_in_db(self, symbol_clean, fb_client)

        if not ticker_id:
            logging.error(f"❌ [ЯДРО v3.0 ОШИБКА]: Паспортистка не смогла легализовать символ '{symbol_clean}'")
            return (None, None)

        # ─── ШАГ 3: СКАНДИРОВАНИЕ СУЩЕСТВУЮЩЕГО ЛИСТИНГА БРОКЕРА ───
        listing_id = None
        if broker_id is not None:
            sql_check_listing = f"""
                SELECT id FROM public.listings 
                WHERE ticker_id = {int(ticker_id)} AND broker_id = {int(broker_id)} LIMIT 1;
            """
            res_listing = self.execute_query(sql_check_listing)
            if res_listing and isinstance(res_listing, list) and len(res_listing) > 0:
                row_l = res_listing[0] if isinstance(res_listing, list) else res_listing
                listing_id = int(row_l.get('id') if isinstance(row_l, dict) else row_l)
                logging.info(f"🎯 [ЯДРО v3.0 КЭШ-L]: Найден существующий листинг LST_ID = {listing_id}")

        # ─── ШАГ 4: АТОМАРНОЕ МЯГКОЕ СОЗДАНИЕ ЛИСТИНГА (СТРОГО ДЛЯ РОЛИ LST) ───
        if listing_id is None and role_clean == "LST" and broker_id is not None:
            # Вызываем наш новый, вынесенный наружу универсальный метод регистрации
            listing_id = self.ensure_listing(
                ticker_id=ticker_id,
                broker_id=broker_id,
                fb_client=fb_client
            )

        # ─── ШАГ 5: КОНТУР РЕГИСТРАТОРА (ЧИСТАЯ И ПОНЯТНАЯ РАЗВИЛКА ДЛЯ ВСЕХ 4 РОЛЕЙ) ───
        if role_clean == "LST":
            source_key = f"LST_ID={listing_id}"
        elif role_clean == "MS":
            source_key = f"MS_{caller_id}"
        else:
            # Для TG_USR и ETF_LT ключ соберется автоматически (напр. TG_USR_ID=1 или ETF_LT_ID=345)
            source_key = f"{role_clean}_ID={caller_id}"

        manage_provenance(self, ticker_id=ticker_id, source_key=source_key, action="add")

        # ─── ШАГ 6: ПОСЛЕДОВАТЕЛЬНЫЙ ПИНОК СБОРЩИКАМ (СТРОГО ДЛЯ РЕАЛЬНО НОВЫХ БУМАГ) ───
        # Жесткий заградительный щит: пинаем сборщиков только для живых контуров (TG_USR, LST)
        # и только если бумага реально новая для базы данных (ticker_id не был найден в локальном кэше на старте)!
        # Для пакетных роботов ETF_LT и MS этот шаг полностью блокируется, спасая ресурсы и IP от банов Yahoo.
        if role_clean in ("TG_USR", "LST") and (res_ticker is None or len(res_ticker) == 0):
            try:
                from site_connectors.sync_signals_yf import sync_global_yahoo_signals
                from site_connectors.sync_fundamentals_yhoo import sync_fundamentals
                
                logging.info(f"🚀 [ЯДРО v3.0]: Запуск последовательного экспресс-скоринга для ticker_id = {ticker_id}")
                sync_global_yahoo_signals(single_ticker_id=ticker_id)
                sync_fundamentals(self, single_ticker_id=ticker_id)
                
            except Exception as sync_trigger_err:
                logging.error(f"⚠️ [ЯДРО v3.0]: Сбой фонового триггера сборщиков Yahoo: {sync_trigger_err}")

        return int(ticker_id), (int(listing_id) if listing_id is not None else None)

    def ensure_watchlist_row_v2(self, portfolio_id: int, listing_id: int, reason: str = "watched"):
        """
        📋 УМНЫЙ РЕГИСТРАТОР ВАТЧЛИСТА v2.0
        Гарантирует присутствие бумаги в портфеле с фиксацией контекста (причины) её появления.
        Защищает внутренний счетчик ID СУБД от холостой накрутки при повторных вызовах.
        
        Допустимые значения параметра reason (строго 4 контекста):
            'watched'  - Бумага поставлена на наблюдение (алерты брокера, ручной фокус).
            'ordered'  - По бумаге выставлен активный приказ/ордер (ожидание исполнения).
            'bought'   - Совершена сделка покупки (бумага зашла в реальный баланс).
            'sold_out' - Позиция полностью закрыта/продана (выход из актива).
        """
        import logging
        
        p_id = int(portfolio_id)
        l_id = int(listing_id)
        reason_clean = str(reason).strip().lower()
        
        # Строгая карта соответствия причин и целевых колонок в СУБД UPort
        reason_columns = {
            "watched": "watched_at",
            "ordered": "ordered_at",
            "bought": "bought_at",
            "sold_out": "sold_out_at"
        }
        
        if reason_clean not in reason_columns:
            logging.error(f"❌ [WATCHLIST v2]: Недопустимый контекст reason='{reason}'. Допускаются только: {list(reason_columns.keys())}")
            return
            
        target_column = reason_columns[reason_clean]
        
        # 🛡️ ЗАЩИТА СПИДОМЕТРА ID: Сначала зряче проверяем, существует ли уже запись
        sql_check = f"SELECT id FROM public.watchlist WHERE portfolio_id = {p_id} AND listing_id = {l_id} LIMIT 1;"
        res_wl = self.execute_query(sql_check)
        
        if res_wl and len(res_wl) > 0:
            # --- ВЕТКА UPDATE: Запись уже есть, точечно взводим нужную метку времени ---
            row_id = int(res_wl[0]['id'])
            sql_update = f"""
                UPDATE public.watchlist 
                SET {target_column} = CURRENT_TIMESTAMP(0),
                    updated_at = CURRENT_TIMESTAMP(0)
                WHERE id = {row_id};
            """
            self.execute_query(sql_update)
            logging.info(f"📝 [WATCHLIST v2]: Бумага LST_ID={l_id} в портфеле ID={p_id} актуализирована по причине '{reason_clean}' (ID строки: {row_id}).")
        else:
            # --- ВЕТКА INSERT: Абсолютно новая запись, заполняем стартовую колонку, остальные NULL ---
            columns_list = ["portfolio_id", "listing_id", "updated_at", target_column]
            values_list = [str(p_id), str(l_id), "CURRENT_TIMESTAMP(0)", "CURRENT_TIMESTAMP(0)"]
            
            # Дописываем остальные колонки времени как NULL, чтобы запрос был монолитным
            for col in reason_columns.values():
                if col != target_column:
                    columns_list.append(col)
                    values_list.append("NULL")
                    
            sql_insert = f"""
                INSERT INTO public.watchlist ({', '.join(columns_list)})
                VALUES ({', '.join(values_list)});
            """
            self.execute_query(sql_insert)
            logging.info(f"➕ [WATCHLIST v2]: Бумага LST_ID={l_id} впервые добавлена в портфель ID={p_id} по причине '{reason_clean}'.")

    def ensure_listing(self, ticker_id: int, broker_id: int, fb_client = None) -> int:
        """
        📈 УНИВЕРСАЛЬНЫЙ РЕГИСТРАТОР ЛИСТИНГОВ v1.0
        Гарантирует присутствие бумаги в таблице listings брокера с получением живой цены.
        Выбрасывает ValueError, если инструмент не поддерживается выбранным брокером.
        """
        t_id = int(ticker_id)
        b_id = int(broker_id)
        
        # 🛡️ ПЕРВИЧНАЯ ПРОВЕРКА КЭША: Если листинг уже существует, мгновенно отдаем его ID
        sql_check = f"SELECT id FROM public.listings WHERE ticker_id = {t_id} AND broker_id = {b_id} LIMIT 1;"
        res_listing = self.execute_query(sql_check)
        if res_listing and len(res_listing) > 0:
            row_l = res_listing[0] if isinstance(res_listing, list) else res_listing
            return int(row_l.get('id') if isinstance(row_l, dict) else row_l)
            
        # 🔗 РЕЛЯЦИОННОЕ ИЗВЛЕЧЕНИЕ КАННОНИЧЕСКОГО ИМЕНИ ИЗ КАРТЫ СУБД
        sql_get_canonical = f"""
            SELECT ticker_name_map ->> (SELECT short_name FROM public.brokers WHERE id = {b_id}) AS broker_symbol
            FROM public.tickers 
            WHERE id = {t_id};
        """
        try:
            res_canonical = self.execute_query(sql_get_canonical)
            row_c = res_canonical[0] if isinstance(res_canonical, list) else res_canonical
            broker_ticker_name = row_c.get('broker_symbol') if isinstance(row_c, dict) else row_c
        except Exception as db_json_err:
            logging.error(f"❌ Ошибка SQL-парсинга JSONB-карты имен в ensure_listing: {db_json_err}")
            broker_ticker_name = None

        # 🛑 ШЛАГБАУМ БЕЗОПАСНОСТИ: Если брокер не поддерживает бумагу — выбрасываем исключение
        if not broker_ticker_name or str(broker_ticker_name).strip().upper() == "UNSUPPORTED":
            raise ValueError("Этот инструмент не торгуется через выбранного брокера")

        broker_ticker_name = str(broker_ticker_name).strip().upper()
        target_currency = "USD"  # Дефолт
        live_price = 0.0         # Стартовая цена по умолчанию
        
        # Контур сбора параметров с биржи под конкретного брокера
        if b_id == 1:
            try:
                if not fb_client:
                    from brokers_connectors.fb_client import FreedomBrokerClient
                    pub_key = os.getenv("FB_DLM_API_KEY")
                    priv_key = os.getenv("FB_DLM_API_SECRET")
                    if pub_key and priv_key:
                        fb_client = FreedomBrokerClient(pub_key, priv_key)
                
                if fb_client:
                    # Запрашиваем справочную валюту по каноническому имени (напр. 'RKLB.US')
                    sec_info = fb_client.get_security_info(broker_ticker_name)
                    if isinstance(sec_info, dict) and sec_info.get("currency"):
                        target_currency = str(sec_info["currency"]).strip().upper()
                    
                    # Сразу забираем живую котировку float через наш универсальный метод
                    live_price = fb_client.get_quotes(broker_ticker_name)
                    logging.info(f"📊 [ENSURE LISTING]: Получена стартовая котировка для {broker_ticker_name}: {live_price}")
                    
            except Exception as api_err:
                logging.error(f"⚠️ [ENSURE LISTING]: Ошибка запроса параметров через робота ФБ: {api_err}")
        elif b_id == 2:
            # Контур Trading 212
            target_currency = "GBP"
            live_price = 0.0
            
        self.ensure_currency(target_currency)
        
        # Фиксируем листинг с правильным именем и живой котировкой
        sql_insert_listing = f"""
            INSERT INTO public.listings (ticker_id, broker_id, broker_symbol, currency_id, last_price)
            VALUES ({t_id}, {b_id}, '{broker_ticker_name}', '{target_currency}', {float(live_price)})
            ON CONFLICT (broker_id, broker_symbol) DO NOTHING;
        """
        self.execute_query(sql_insert_listing)
        
        res_id = self.execute_query(
            f"SELECT id FROM public.listings WHERE broker_id = {b_id} AND broker_symbol = '{broker_ticker_name}' LIMIT 1;"
        )
        if res_id and len(res_id) > 0:
            # 🔥 ФИКС v5.3: Зряче извлекаем нулевой элемент списка словарей PostgreSQL
            row_id = res_id[0] if isinstance(res_id, list) else res_id
            listing_id = int(row_id.get('id') if isinstance(row_id, dict) else row_id)
            
            # Фоновый запуск декомпозиции без блокировки Event Loop
            try:
                sql_get_type = f"SELECT asset_type FROM public.tickers WHERE id = {t_id};"
                res_type = self.execute_query(sql_get_type)
                if res_type and len(res_type) > 0:
                    row_t = res_type[0] if isinstance(res_type, list) else res_type
                    asset_type = str(row_t.get('asset_type', 'UNDETERMINED')).strip().upper()
                    
                    if asset_type == "ETF":
                        from site_connectors.trigger_etf_look_through import run_etf_look_through_decomposition
                        logging.info(f"🔬 [ЯДРО - LISTING]: Обнаружен листинг фонда ETF! Порождаю фоновую задачу декомпозиции для ticker_id = {t_id}")
                        asyncio.create_task(run_etf_look_through_decomposition(target_ticker_id=t_id))
            except Exception as etf_trigger_err:
                logging.error(f"⚠️ [ЯДРО - LISTING]: Сбой фонового запуска декомпозиции ETF: {etf_trigger_err}")

            return listing_id
            
        return 0
    # === ОСНОВНОЙ ЭТАП ЗАПИСИ ПРИКАЗОВ ===

    @staticmethod
    def sync_portfolio_orders(portfolio_id: int, account_number: str, api_orders: list):
        """
        Этап 2: Полностью перезаписывает слепок активных приказов по портфелю.
        Вводит заполнение реляционного listing_id в таблице orders.
        """
        sql_delete_orders = "DELETE FROM public.orders WHERE portfolio_id = %s;"
        
        # Получаем одновременно ticker_id и listing_id на основе broker_symbol
        sql_get_ids = """
            SELECT l.id as listing_id, l.ticker_id 
            FROM public.listings l
            JOIN public.portfolios p ON l.broker_id = p.broker_id
            WHERE p.id = %s AND l.broker_symbol = %s;
        """
        
        # Запрос адаптирован: записывает как старый ticker_id, так и новый listing_id
        sql_insert_order = """
            INSERT INTO public.orders (portfolio_id, ticker_id, listing_id, broker_order_id, status, oper, type, q, p, stop_init_price, stop_price, currency_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        
        sql_reset_all_reserved = "UPDATE public.accounts SET cash_reserved = 0 WHERE account_number = %s;"
        sql_update_cash_reserved = "UPDATE public.accounts SET cash_reserved = %s WHERE account_number = %s AND currency_id = %s;"

        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASS")
        )
        
        try:
            with conn.cursor() as cur:
                cur.execute(sql_delete_orders, (portfolio_id,))
                
                for order in api_orders:
                    cur.execute(sql_get_ids, (portfolio_id, order['ticker']))
                    row = cur.fetchone()
                    
                    if row:
                        listing_id = row[0]
                        ticker_id = row[1]
                    else:
                        # Резервный фолбэк на случай рассинхронизации справочников
                        cur.execute("SELECT id FROM public.tickers WHERE full_ticker = %s;", (order['ticker'],))
                        t_fallback = cur.fetchone()
                        ticker_id = t_fallback[0] if t_fallback else 1
                        listing_id = None

                    stop_price_val = order.get('stop')                    
                    if stop_price_val is None and int(order.get('type', 0)) in (3, 4, 5, 6):
                        stop_price_val = order.get('stop_init_price')
                    if stop_price_val is not None:
                        stop_price_val = float(stop_price_val)
                    
                    cur.execute(sql_insert_order, (
                        portfolio_id,
                        ticker_id,
                        listing_id,
                        order['broker_order_id'],
                        order['status'],
                        order['oper'],
                        order['type'],
                        order['q'],
                        order['p'],
                        order['stop_init_price'],
                        stop_price_val,
                        order['currency_id']
                    ))
                
                cur.execute(sql_reset_all_reserved, (account_number,))
                currency_reserves = {}
                for order in api_orders:
                    if order['type'] == 2 and order['oper'] in (1, 2):
                        order_cost = order['q'] * order['p']
                        curr = order['currency_id']
                        currency_reserves[curr] = currency_reserves.get(curr, 0) + order_cost
            
                for curr_id, reserved_amount in currency_reserves.items():
                    cur.execute(sql_update_cash_reserved, (reserved_amount, account_number, curr_id))
                    
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"Ошибка транзакции sync_portfolio_orders: {e}")
            raise e
        finally:
            conn.close()

    # === СЕМЕЙНАЯ СВОДКА ===

    def get_family_summary(self, telegram_id: int) -> dict:
        """
        Собирает общую сводку капитала семьи в транзитных USD и переводит в валюту пользователя.
        """
        user_sql = f"SELECT id, base_currency FROM public.users WHERE telegram_id = {telegram_id};"
        user_res = self.execute_query(user_sql)
        if not user_res or not isinstance(user_res, list):
            return {}
        
        current_user = user_res[0]
        user_id = current_user['id']
        base_curr = current_user['base_currency'] or "USD"

        sign_sql = f"SELECT sign FROM public.currencies WHERE id = '{base_curr}';"
        sign_res = self.execute_query(sign_sql)
        curr_sign = sign_res[0]['sign'] if sign_res else base_curr

        rate_user_sql = f"SELECT rate FROM public.currency_rates WHERE from_currency = '{base_curr}' AND to_currency = 'USD';"
        rate_user_res = self.execute_query(rate_user_sql)
        user_to_usd_rate = float(rate_user_res[0]['rate']) if rate_user_res else 1.0

        accounts_sql = """
            SELECT
                acc.user_id, acc.portfolio_id, acc.broker_id, acc.cash_available, acc.assets_value,
                p.name as portfolio_name, COALESCE(r.rate, 1.0) as to_usd_rate
            FROM public.accounts acc
            LEFT JOIN public.portfolios p ON acc.portfolio_id = p.id
            LEFT JOIN public.currency_rates r ON r.from_currency = acc.currency_id AND r.to_currency = 'USD';
        """
        all_accounts = self.execute_query(accounts_sql)
        if not isinstance(all_accounts, list):
            all_accounts = []

        total_assets_usd = 0.0
        total_cash_usd = 0.0
        portfolios_dict = {}

        for acc in all_accounts:
            rate = float(acc['to_usd_rate'])
            total_assets_usd += float(acc['assets_value'] or 0) * rate
            total_cash_usd += float(acc['cash_available'] or 0) * rate

            p_id = acc['portfolio_id']
            if p_id and p_id not in portfolios_dict:
                portfolios_dict[p_id] = {
                    "id": p_id,
                    "name": acc['portfolio_name'] or f"Портфель #{p_id}",
                    "is_owner": (acc['user_id'] == user_id),
                    "owner_id": acc['user_id'],
                    "broker_id": acc['broker_id']
                }

        final_assets = total_assets_usd / user_to_usd_rate
        final_cash = total_cash_usd / user_to_usd_rate

        sorted_portfolios = sorted(
            portfolios_dict.values(),
            key=lambda x: 0 if x['is_owner'] else 1
        )

        return {
            "base_currency": base_curr,
            "currency_sign": curr_sign,
            "total_assets": final_assets,
            "total_cash": final_cash,
            "portfolios": sorted_portfolios,
            "user_id": user_id,
            "user_to_usd_rate": user_to_usd_rate
        }

    # === ОБНОВЛЕННЫЙ МЕТОД ДЛЯ КАРТОЧКИ АКТИВА (v3.0 АКАДЕМИЧЕСКИЙ) ===

    def get_ticker_context(self, full_ticker: str, portfolio_id: int = 0, telegram_id: int = None) -> dict:
        """
        Собирает контекст по тикеру на основе реляционной связки через таблицу listings.
        """
        base_curr = "USD"
        if telegram_id:
            user_sql = f"SELECT base_currency FROM public.users WHERE telegram_id = {telegram_id};"
            user_res = self.execute_query(user_sql)
            if user_res and isinstance(user_res, list):
                base_curr = user_res[0]['base_currency'] or "USD"

        rate_user_sql = f"SELECT rate FROM public.currency_rates WHERE from_currency = '{base_curr}' AND to_currency = 'USD';"
        rate_user_res = self.execute_query(rate_user_sql)
        user_to_usd_rate = float(rate_user_res[0]['rate']) if rate_user_res and isinstance(rate_user_res, list) else 1.0

        # ЗАПРОС 1: Базовые рыночные параметры листинга из точки истины listings -> tickers
        ticker_sql = f"""
            SELECT l.id as listing_id, l.broker_symbol as full_ticker, t.company_name, l.last_price, l.currency_id AS ticker_currency,
                   l.last_updated_at, 'active' as tracking_status, 'Система' AS added_by_user,
                   COALESCE(r.rate, 1.0) as asset_to_usd_rate
            FROM public.listings l
            JOIN public.tickers t ON l.ticker_id = t.id
            LEFT JOIN public.currency_rates r ON r.from_currency = l.currency_id AND r.to_currency = 'USD'
            WHERE l.broker_symbol = '{full_ticker.strip().upper()}';
        """
        ticker_res = self.execute_query(ticker_sql)
        if not ticker_res or not isinstance(ticker_res, list):
            return {}

        ticker_data = ticker_res[0]
        listing_id = int(ticker_data['listing_id'])
        raw_price = float(ticker_data['last_price'] or 0)
        asset_to_usd = float(ticker_data['asset_to_usd_rate'])
        price_in_user_currency = (raw_price * asset_to_usd) / user_to_usd_rate

        # ЗАПРОС 2: Контекст владения по новому listing_id
        portfolio_filter_assets = "" if portfolio_id == 0 else f"AND a.portfolio_id = {portfolio_id}"
        
        assets_sql = f"""
            SELECT p.name AS portfolio_name, u.name AS owner_name, a.quantity, a.avg_price, l.currency_id,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - a.position_opened_at)) AS holding_days,
                   a.position_opened_at::date AS opened_date, COALESCE(r.rate, 1.0) as cash_to_usd_rate
            FROM public.assets a
            JOIN public.listings l ON a.listing_id = l.id
            JOIN public.portfolios p ON a.portfolio_id = p.id
            JOIN public.users u ON p.owner_id = u.id
            LEFT JOIN public.currency_rates r ON r.from_currency = l.currency_id AND r.to_currency = 'USD'
            WHERE a.listing_id = {listing_id} AND a.quantity > 0 {portfolio_filter_assets};
        """
        assets_res = self.execute_query(assets_sql)
        if not isinstance(assets_res, list):
            assets_res = []

        ownership_list = []
        for asset in assets_res:
            raw_avg = float(asset['avg_price'] or 0)
            cash_to_usd = float(asset['cash_to_usd_rate'])
            avg_in_user_currency = (raw_avg * cash_to_usd) / user_to_usd_rate

            ownership_list.append({
                "portfolio_name": asset['portfolio_name'],
                "owner_name": asset['owner_name'],
                "quantity": float(asset['quantity']),
                "avg_price": avg_in_user_currency,
                "holding_days": int(asset['holding_days'] or 0),
                "opened_date": str(asset['opened_date'])
            })

        # ЗАПРОС 3: Контекст активных ордеров по listing_id
        portfolio_filter_orders = "" if portfolio_id == 0 else f"AND o.portfolio_id = {portfolio_id}"
        
        orders_sql = f"""
            SELECT p.name AS portfolio_name, o.broker_order_id, o.status, o.q AS order_quantity, o.p AS order_price,
                   l.currency_id, o.oper, o.created_at, COALESCE(r.rate, 1.0) as order_to_usd_rate
            FROM public.orders o
            JOIN public.listings l ON o.listing_id = l.id
            JOIN public.portfolios p ON o.portfolio_id = p.id
            LEFT JOIN public.currency_rates r ON r.from_currency = l.currency_id AND r.to_currency = 'USD'
            WHERE o.listing_id = {listing_id} AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED') {portfolio_filter_orders};
        """
        orders_res = self.execute_query(orders_sql)
        if not isinstance(orders_res, list):
            orders_res = []

        orders_list = []
        for ord_row in orders_res:
            raw_order_p = float(ord_row['order_price'] or 0)
            ord_to_usd = float(ord_row['order_to_usd_rate'])
            order_p_in_user_currency = (raw_order_p * ord_to_usd) / user_to_usd_rate

            orders_list.append({
                "portfolio_name": ord_row['portfolio_name'],
                "broker_order_id": ord_row['broker_order_id'],
                "status": ord_row['status'],
                "quantity": float(ord_row['order_quantity']),
                "price": order_p_in_user_currency,
                "operation": "ПОКУПКА" if ord_row['oper'] in (1, 2) else "ПРОДАЖА",
                "created_at": str(ord_row['created_at']).split('.')[0]
            })

        return {
            "is_global_view": (portfolio_id == 0),
            "base_currency": base_curr,
            "full_ticker": ticker_data['full_ticker'],
            "company_name": ticker_data['company_name'] or "Неизвестная компания",
            "last_price": price_in_user_currency,
            "tracking_status": ticker_data['tracking_status'],
            "added_by_user": ticker_data['added_by_user'] or "Система",
            "last_updated_at": str(ticker_data['last_updated_at']).split('.')[0],
            "ownership": ownership_list,
            "active_orders": orders_list
        }

    # ЗАПРОС: Контекст алертов 
    def get_ticker_alerts_context(self, listing_id: int) -> list:
        """
        Метод Академического контура алертов v1.0.
        Выгружает все активные и исторические триггеры по конкретному listing_id,
        подтягивая имена владельцев портфелей и создателей алертов через реляционный слой.
        """
        sql = f"""
            SELECT 
                al.id, al.portfolio_id, al.source_type, al.trigger_type, al.condition_type, 
                al.trigger_price, al.trigger_price_min, al.trigger_price_max, al.trigger_pct,
                al.is_active, al.triggered_status, al.triggered_at, al.note, al.ai_strategy_id,
                p.name AS portfolio_name,
                u_owner.name AS portfolio_owner_name,
                u_creator.name AS creator_name
            FROM public.alerts al
            JOIN public.portfolios p ON al.portfolio_id = p.id
            JOIN public.users u_owner ON p.owner_id = u_owner.id
            LEFT JOIN public.users u_creator ON al.created_by_user_id = u_creator.id
            WHERE al.listing_id = {int(listing_id)}
            ORDER BY al.is_active DESC, al.created_at DESC;
        """
        res = self.execute_query(sql)
        return res if isinstance(res, list) else ([res] if res else [])



# Глобальные изолированные инстансы базы данных для всей экосистемы UPort
db_bot = Database(role="BOT")       # read only       
db_sys = Database(role="SYSTEM")    # read/write
