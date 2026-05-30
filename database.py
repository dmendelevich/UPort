import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from pathlib import Path
import asyncio

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

    def ensure_ticker(self, full_ticker: str, currency_id: str, broker_id: int = 1):
        """
        Кит №3 (Legacy-совместимый): Оставлен для фоновой работы старого кода до полной миграции.
        """
        symbol, suffix = full_ticker.split(".", 1) if "." in full_ticker else (full_ticker, "US")
        sql = f"""
        INSERT INTO public.tickers (symbol, suffix, full_ticker, currency_id, broker_id)
        VALUES ('{symbol}', '{suffix}', '{full_ticker}', '{currency_id}', {broker_id})
        ON CONFLICT (full_ticker) DO NOTHING;
        """
        self.execute_query(sql)
        try:
            t_info = self.execute_query(f"SELECT id FROM public.tickers WHERE full_ticker = '{full_ticker}';")
            if t_info:
                t_row = t_info[0] if isinstance(t_info, list) and len(t_info) > 0 else (t_info if isinstance(t_info, dict) else {})
                t_id = t_row.get('id')
                if t_id:
                    check_h = self.execute_query(f"SELECT 1 FROM public.etf_holdings WHERE etf_ticker_id = {t_id} LIMIT 1;")
                    if not check_h:
                        task_data = {"id": t_id, "symbol": symbol, "suffix": suffix, "full_ticker": full_ticker, "currency_id": currency_id, "broker_id": broker_id}
                        ETF_LOOK_THROUGH_QUEUE.put_nowait(task_data)
        except Exception as q_err:
            print(f"⚠️ Предупреждение постановки {full_ticker} в ETF очередь: {q_err}")

    def ensure_ticker_v2(self, broker_id: int, broker_symbol: str, fallback_currency: str = "USD", fb_client = None) -> tuple:
        """
        Кит №3 (Академический v3.0): Гарантирует наличие эмитента в tickers и листинга в listings.
        Использует СУП-переводчик имен для Freedom Broker и кэширование СУБД.
        Возвращает кортеж: (ticker_id, listing_id)
        """
        broker_symbol_clean = broker_symbol.strip().upper()
        
        # 1. СНАЙПЕРСКАЯ ПРОВЕРКА КЭША СУБД (Работает за микросекунды)
        sql_check = f"SELECT id, ticker_id FROM public.listings WHERE broker_id = {broker_id} AND broker_symbol = '{broker_symbol_clean}';"
        # 🔥 ФИКС КЭША v3.1: Извлекаем первый словарь из списка ответов шлюза
        cache_res = self.execute_query(sql_check)
        if cache_res and isinstance(cache_res, list) and len(cache_res) > 0:
            row = cache_res[0]  # Берем первый словарь из массива!
            if isinstance(row, dict) and row.get('id') and row.get('ticker_id'):
                return int(row['ticker_id']), int(row['id'])

        # 2. ИНИЦИАЛИЗАЦИЯ ДЕФОЛТНЫХ ЗНАЧЕНИЙ НА СЛУЧАЙ СБОЯ API БРОКЕРА
        symbol = broker_symbol_clean.split(".", 1)[0] if "." in broker_symbol_clean else broker_symbol_clean
        isin = "UNKNOWN"
        comp_name = "Unknown Company"
        currency_id = fallback_currency.upper()

        # 3. СУП-ПЕРЕВОДЧИК ИМЕН ДЛЯ FREEDOM BROKER (ID = 1)
        if broker_id == 1 and fb_client is not None:
            try:
                print(f"📡 [Ядро СУП]: Новый инструмент! Запрашиваю спецификацию Freedom Broker для '{broker_symbol_clean}'...")
                sec_info = fb_client.get_security_info(broker_symbol_clean)
                
                if sec_info and isinstance(sec_info, dict):
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
                print(f"⚠️ [Ядро СУП WARNING]: Не удалось получить данные из СУП FB: {sup_err}")

        # 4. СИНХРОНИЗАЦИЯ ГЛОБАЛЬНОГО СПРАВОЧНИКА (public.tickers)
        self.ensure_currency(currency_id)
        
        # Переносим брокерский full_ticker для legacy-поддержки старых кусков системы
        suffix = broker_symbol_clean.split(".", 1)[1] if "." in broker_symbol_clean else "US"
        sql_insert_ticker = f"""
            INSERT INTO public.tickers (symbol, suffix, full_ticker, currency_id, broker_id, isin, company_name)
            VALUES ('{symbol}', '{suffix}', '{broker_symbol_clean}', '{currency_id}', {broker_id}, '{isin}', '{comp_name}')
            ON CONFLICT (full_ticker) 
            DO UPDATE SET isin = CASE WHEN public.tickers.isin = 'UNKNOWN' THEN EXCLUDED.isin ELSE public.tickers.isin END,
                          company_name = CASE WHEN public.tickers.company_name IS NULL OR public.tickers.company_name = 'Unknown Company' OR public.tickers.company_name = '' THEN EXCLUDED.company_name ELSE public.tickers.company_name END
            RETURNING id;
        """
        t_res = self.execute_query(sql_insert_ticker)
        t_row = t_res[0] if t_res and isinstance(t_res, list) and len(t_res) > 0 else (t_res if isinstance(t_res, dict) else {})
        ticker_id = t_row.get('id')
        
        if not ticker_id:
            # Резервный SELECT на случай race condition с безопасным извлечением [0]
            t_get = self.execute_query(f"SELECT id FROM public.tickers WHERE full_ticker = '{broker_symbol_clean}';")
            t_get_row = t_get[0] if t_get and isinstance(t_get, list) and len(t_get) > 0 else (t_get if isinstance(t_get, dict) else {})
            ticker_id = t_get_row.get('id')

        if not ticker_id:
            raise RuntimeError(f"Критическая ошибка ядра: Не удалось сгенерировать ticker_id для {broker_symbol_clean}")

        # 5. СИНХРОНИЗАЦИЯ ТАБЛИЦЫ-ПЕРЕСЕЧЕНИЯ (public.listings)
        sql_insert_listing = f"""
            INSERT INTO public.listings (ticker_id, broker_id, broker_symbol, currency_id)
            VALUES ({ticker_id}, {broker_id}, '{broker_symbol_clean}', '{currency_id}')
            ON CONFLICT (broker_id, broker_symbol) DO NOTHING;
        """
        self.execute_query(sql_insert_listing)
        
        # Безопасный забор сгенерированного listing_id с извлечением [0]
        l_res = self.execute_query(f"SELECT id FROM public.listings WHERE broker_id = {broker_id} AND broker_symbol = '{broker_symbol_clean}';")
        l_row = l_res[0] if l_res and isinstance(l_res, list) and len(l_res) > 0 else (l_res if isinstance(l_res, dict) else {})
        listing_id = l_row.get('id')

        if not listing_id:
            raise RuntimeError(f"Критическая ошибка ядра: Не удалось сгенерировать listing_id для {broker_symbol_clean}")

        # 6. ПОСТАНОВКА В ФОНОВУЮ ОЧЕРЕДЬ ETF LOOK-THROUGH
        try:
            check_h = self.execute_query(f"SELECT 1 FROM public.etf_holdings WHERE etf_ticker_id = {ticker_id} LIMIT 1;")
            if not check_h:
                task_data = {"id": ticker_id, "symbol": symbol, "suffix": suffix, "full_ticker": broker_symbol_clean, "currency_id": currency_id, "broker_id": broker_id}
                ETF_LOOK_THROUGH_QUEUE.put_nowait(task_data)
        except Exception as q_err:
            print(f"⚠️ Предупреждение постановки {symbol} в ETF очередь: {q_err}")

        # 🔍 ТЩАТЕЛЬНАЯ ДИАГНОСТИКА ЯДРА ПЕРЕД ВОЗВРАТОМ
        print(f"🧪 [ДЕБАГ ЯДРА]: Текстовый символ: '{broker_symbol_clean}' | ID Тикера (тип {type(ticker_id)}): {ticker_id} | ID Листинга (тип {type(listing_id)}): {listing_id}")
        
        try:
            return int(ticker_id), int(listing_id)
        except Exception as int_err:
            print(f"🚨 [КРИТ ВСПЫШКА В ЯДРЕ]: Ошибка приведения к int! ticker_id={ticker_id}, listing_id={listing_id}. Ошибка: {int_err}")
            raise int_err



    def ensure_watchlist_row(self, portfolio_id: int, listing_id: int, source_type: str = "user"):
        """Гарантирует, что бумага присутствует в списке наблюдения со статусом considered."""
        sql = f"""
            INSERT INTO public.watchlist (portfolio_id, listing_id, status, source_type)
            VALUES ({portfolio_id}, {listing_id}, 'considered'::public.ticker_lifecycle_status, '{source_type}')
            ON CONFLICT (portfolio_id, listing_id) DO NOTHING;
        """
        self.execute_query(sql)

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
                acc.user_id, acc.portfolio_id, acc.cash_available, acc.assets_value,
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
                    "is_owner": (acc['user_id'] == user_id)
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

# Глобальные изолированные инстансы базы данных для всей экосистемы UPort
db_bot = Database(role="BOT")       
db_sys = Database(role="SYSTEM")
