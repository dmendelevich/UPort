import os
import requests
from dotenv import load_dotenv
from pathlib import Path

class Database:
    def __init__(self, role: str = "BOT"):
        """
        Универсальный класс взаимодействия со шлюзом UPort AI Gateway.
        Доступные роли: 'SYSTEM', 'BOT', 'AI'. По умолчанию: 'BOT' (для совместимости).
        """
        env_path = Path('/root/UPort/.env')
        load_dotenv(dotenv_path=env_path)
        
        # Точный URL локального шлюза
        self.url = "http://localhost:3000/query"
        
        # Динамический выбор токена в зависимости от запрашиваемой роли
        if role == "SYSTEM":
            self.token = os.getenv("UPORT_TOKEN_SYSTEM")
        elif role == "AI":
            self.token = os.getenv("UPORT_TOKEN_AI")
        elif role == "BOT":
            self.token = os.getenv("UPORT_TOKEN_BOT")
        else:
            raise ValueError(f"Неизвестная роль базы данных: {role}")
            
        if not self.token:
            raise RuntimeError(f"Критическая ошибка: Токен для роли {role} не найден в .env файле.")

    def execute_query(self, sql_query: str) -> list:
        """Отправка SQL-запроса на шлюз с заголовком авторизации текущей роли."""
        headers = {"X-Token": self.token}
        payload = {"query": sql_query}
        
        try:
            response = requests.post(self.url, json=payload, headers=headers, timeout=10)
            if response.status_code != 200:
                print(f"DEBUG DB [{self.token[:5]}...]: Шлюз вернул {response.status_code}: {response.text}")
                return []
            return response.json()
        except Exception as e:
            print(f"Ошибка подключения к шлюзу базы данных: {e}")
            return []

    def get_portfolio_data_by_email(self, email: str) -> dict:
        sql = f"""
            SELECT p.id, p.name as portfolio_name, u.name as owner_name
            FROM portfolios p 
            JOIN users u ON p.owner_id = u.id
            JOIN user_emails ue ON u.id = ue.user_id 
            WHERE ue.email = '{email}'
        """
        result = self.execute_query(sql)
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None
    
    def get_assets_for_reconciliation(self, portfolio_id: int) -> list:
        sql = f"""
            SELECT t.full_ticker, a.quantity
            FROM assets a
            JOIN tickers t ON a.ticker_id = t.id
            WHERE a.portfolio_id = {portfolio_id}
        """
        result = self.execute_query(sql)
        return result if result else []

    def save_raw_email(self, broker_id: int, user_id: int, email_to: str, subject: str, body: str, internal_hash: str, email_date: str) -> list:
        sql = f"""
        INSERT INTO incoming_messages (broker_id, user_id, source_type, subject, body_content, internal_hash, status, email_datetime)
        VALUES ({broker_id}, {user_id}, 'EMAIL', '{subject}', '{body}', '{internal_hash}', 'pending', '{email_date}')
        """
        return self.execute_query(sql)

    def sync_portfolio_orders(portfolio_id: int, account_number: str, api_orders: list):
    """
    Полностью перезаписывает слепок активных приказов по портфелю.
    Автоматически расширяет таблицы currencies и accounts при появлении новых валют.
    Пересчитывает заблокированный кэш (cash_reserved) для всех субсчетов.
    """
    # 1. Запросы для работы с приказами
    sql_delete_orders = "DELETE FROM public.orders WHERE portfolio_id = %s;"
    
    sql_get_ticker_id = "SELECT id FROM public.tickers WHERE full_ticker = %s;"
    
    sql_insert_order = """
        INSERT INTO public.orders (portfolio_id, ticker_id, broker_order_id, order_type, status, quantity_ordered, price_limit, currency_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
    """
    
    # 2. Запросы для динамического создания валют и субсчетов (Защита от сбоев)
    sql_ensure_currency = """
        INSERT INTO public.currencies (id, sign, multiplier)
        VALUES (%s, %s, 1.0)
        ON CONFLICT (id) DO NOTHING;
    """
    
    # Чтобы создать новый субсчет, нам нужны метаданные (user_id, broker_id, account_type) уже существующего счета.
    # Берем их из первой попавшейся строчки с этим же номером счета.
    sql_get_account_meta = """
        SELECT user_id, broker_id, account_type 
        FROM public.accounts 
        WHERE account_number = %s 
        LIMIT 1;
    """
    
    sql_ensure_account_sub_row = """
        INSERT INTO public.accounts (user_id, portfolio_id, broker_id, account_number, account_type, currency_id, cash_available, cash_reserved)
        VALUES (%s, %s, %s, %s, %s, %s, 0, 0)
        ON CONFLICT (account_number, currency_id) DO NOTHING;
    """
    
    # 3. Запросы для обновления денежных балансов
    sql_reset_all_reserved = "UPDATE public.accounts SET cash_reserved = 0 WHERE account_number = %s;"
    sql_update_cash_reserved = "UPDATE public.accounts SET cash_reserved = %s WHERE account_number = %s AND currency_id = %s;"

    conn = get_connection()  # Используем вашу функцию подключения из database.py
    try:
        with conn.cursor() as cur:
            # Шаг 1: Начисто удаляем старые приказы по этому портфелю
            cur.execute(sql_delete_orders, (portfolio_id,))
            
            # Шаг 2: Получаем метаданные счета для возможного создания новых субсчетов
            cur.execute(sql_get_account_meta, (account_number,))
            meta_row = cur.fetchone()
            if not meta_row:
                raise ValueError(f"Счет с номером {account_number} не найден в таблице accounts. Невозможно определить метаданные.")
            
            user_id, broker_id, account_type = meta_row
            
            # Шаг 3: Вставляем новые ордера и готовим структуру под новые валюты
            for order in api_orders:
                # Гарантируем, что валюта есть в справочнике currencies
                cur.execute(sql_ensure_currency, (order['currency_id'], order['currency_id']))
                
                # Гарантируем, что строка субсчета (счет + валюта) создана в accounts
                cur.execute(sql_ensure_account_sub_row, (
                    user_id, portfolio_id, broker_id, account_number, account_type, order['currency_id']
                ))
                
                # Находим внутренний id тикера (например, 'TSM.US')
                cur.execute(sql_get_ticker_id, (order['ticker'],))
                ticker_row = cur.fetchone()
                
                if not ticker_row:
                    # Если тикера нет в базе, пока пропускаем приказ, чтобы не уронить транзакцию.
                    # В будущем сюда можно встроить автоматическое создание тикера.
                    continue
                
                ticker_id = ticker_row[0]
                
                # Вставляем запись о приказе в таблицу orders
                cur.execute(sql_insert_order, (
                    portfolio_id,
                    ticker_id,
                    order['broker_order_id'],
                    order['order_type'],
                    order['status'],
                    order['quantity_ordered'],
                    order['price_limit'],
                    order['currency_id']
                ))
            
            # Шаг 4: Пересчет заблокированных денег (cash_reserved)
            # Сначала полностью обнуляем резерв для абсолютно всех валют этого счета
            cur.execute(sql_reset_all_reserved, (account_number,))
            
            # На уровне Python агрегируем суммы только для лимитных приказов на ПОКУПКУ ('buy')
            currency_reserves = {}
            for order in api_orders:
                if order['order_type'] == 'limit' and order['action'] == 'buy':
                    order_cost = order['quantity_ordered'] * order['price_limit']
                    curr = order['currency_id']
                    currency_reserves[curr] = currency_reserves.get(curr, 0) + order_cost
            
            # Обновляем cash_reserved точечно для каждой валюты, по которой есть активный резерв
            for curr_id, reserved_amount in currency_reserves.items():
                cur.execute(sql_update_cash_reserved, (reserved_amount, account_number, curr_id))
                
        # Если всё прошло успешно, сохраняем изменения в базе данных
        conn.commit()
    except Exception as e:
        # В случае любой ошибки (сбой сети, неверный тип) откатываем базу к исходному состоянию
        conn.rollback()
        print(f"Ошибка транзакции sync_portfolio_orders: {e}")
        raise e
    finally:
        conn.close()
        