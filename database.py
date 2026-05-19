import os
import requests
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

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
                return []
            return response.json()
        except Exception as e:
            print(f"Ошибка подключения к шлюзу базы данных: {e}")
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
        """Кит №3: Гарантирует наличие тикера в справочнике tickers с привязкой к брокеру."""
        symbol, suffix = full_ticker.split(".", 1) if "." in full_ticker else (full_ticker, "US")
        sql = f"""
        INSERT INTO public.tickers (symbol, suffix, full_ticker, currency_id, broker_id)
        VALUES ('{symbol}', '{suffix}', '{full_ticker}', '{currency_id}', {broker_id})
        ON CONFLICT (full_ticker) DO NOTHING;
        """
        self.execute_query(sql)

    # === ОСНОВНОЙ ЭТАП ЗАПИСИ ПРИКАЗОВ ===

    @staticmethod
    def sync_portfolio_orders(portfolio_id: int, account_number: str, api_orders: list):
        """
        Этап 2: Полностью перезаписывает слепок активных приказов по портфелю.
        Выполняется в рамках чистой транзакции. Все связанные сущности уже гарантированно созданы.
        """
        sql_delete_orders = "DELETE FROM public.orders WHERE portfolio_id = %s;"
        sql_get_ticker_id = "SELECT id FROM public.tickers WHERE full_ticker = %s;"
        
        # Запрос перестроен под чистые, оригинальные колонки из JSON (9 параметров)
        sql_insert_order = """
        INSERT INTO public.orders (portfolio_id, ticker_id, broker_order_id, status, oper, type, q, p, stop_init_price, currency_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
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
                # 1. Начисто удаляем старые приказы этого портфеля
                cur.execute(sql_delete_orders, (portfolio_id,))
                
                # 2. Размеренно вставляем все приказы из JSON
                for order in api_orders:
                    cur.execute(sql_get_ticker_id, (order['ticker'],))
                    t_row = cur.fetchone()
                    # Безопасное извлечение ID тикера независимо от типа фабрики курсора
                    ticker_id = t_row['id'] if isinstance(t_row, dict) else t_row[0]
                    
                    cur.execute(sql_insert_order, (
                        portfolio_id,
                        ticker_id,
                        order['broker_order_id'],
                        order['status'],
                        order['oper'],
                        order['type'],
                        order['q'],
                        order['p'],
                        order['stop_init_price'],
                        order['currency_id']
                    ))
                
                # 3. Пересчет заблокированных денег (cash_reserved)
                # По правилам брокера: блокировка идет только для лимитных приказов (type=2) на покупку (oper=1 или 2)
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

    # === НОВЫЙ МЕТОД ДЛЯ СЕМЕЙНОЙ СВОДКИ (ЭТАП 1) ===

    def get_family_summary(self, telegram_id: int) -> dict:
        """
        Собирает общую сводку капитала семьи в транзитных USD и переводит в валюту пользователя.
        Использует готовые агрегированные поля из таблицы accounts.
        """
        # 1. Находим пользователя и его базовую валюту
        user_sql = f"SELECT id, base_currency FROM public.users WHERE telegram_id = {telegram_id};"
        user_res = self.execute_query(user_sql)
        if not user_res or not isinstance(user_res, list):
            return {}
        
        current_user = user_res[0]
        user_id = current_user['id']
        base_curr = current_user['base_currency'] or "USD"

        # 2. Получаем знак валюты пользователя
        sign_sql = f"SELECT sign FROM public.currencies WHERE id = '{base_curr}';"
        sign_res = self.execute_query(sign_sql)
        curr_sign = sign_res[0]['sign'] if sign_res else base_curr

        # 3. Получаем курс базовой валюты пользователя к транзитному USD
        rate_user_sql = f"SELECT rate FROM public.currency_rates WHERE from_currency = '{base_curr}' AND to_currency = 'USD';"
        rate_user_res = self.execute_query(rate_user_sql)
        user_to_usd_rate = float(rate_user_res[0]['rate']) if rate_user_res else 1.0

        # 4. Одним запросом собираем балансы всех счетов семьи и курсы их валют к USD
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

        # 5. Считаем суммы в транзитных USD "в тупую" по курсам из таблицы
        for acc in all_accounts:
            rate = float(acc['to_usd_rate'])
            
            # Агрегируем общие семейные суммы
            total_assets_usd += float(acc['assets_value'] or 0) * rate
            total_cash_usd += float(acc['cash_available'] or 0) * rate

            # Собираем уникальные портфели для кнопок меню
            p_id = acc['portfolio_id']
            if p_id and p_id not in portfolios_dict:
                portfolios_dict[p_id] = {
                    "id": p_id,
                    "name": acc['portfolio_name'] or f"Портфель #{p_id}",
                    "is_owner": (acc['user_id'] == user_id) # Флаг: принадлежит ли текущему юзеру
                }

        # 6. Финальный пересчет: делим долларовые суммы на курс валюты пользователя
        final_assets = total_assets_usd / user_to_usd_rate
        final_cash = total_cash_usd / user_to_usd_rate

        # Сортируем портфели: свои (is_owner=True) всегда идут первыми в списке
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
