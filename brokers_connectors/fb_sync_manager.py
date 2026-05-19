import os
import json
from dotenv import load_dotenv
from pathlib import Path

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
        
        # 1. Проверяем наличие счета в таблице accounts
        acc_sql = f"""
            SELECT a.user_id, a.portfolio_id, a.account_type, u.name as owner_name, u.prefix 
            FROM accounts a
            JOIN users u ON a.user_id = u.id
            WHERE a.account_number = '{account_number}' LIMIT 1
        """
        acc_data = self.db.execute_query(acc_sql)
        
        # АВТОМАТИЧЕСКОЕ СОЗДАНИЕ: Если таблица accounts пуста, генерируем базовую структуру
        if not acc_data or len(acc_data) == 0:
            print(f"♻️ [Sync Manager]: Счет {account_number} не найден в accounts. Автоматическое создание...")
            
            is_deposit = account_number.startswith("D")
            look_num = account_number[1:] if is_deposit else account_number
            account_type = "deposit" if is_deposit else "trade"
            
            user_sql = f"SELECT id, name, prefix, id as user_id FROM users WHERE account_number = '{look_num}' LIMIT 1"
            user_rows = self.db.execute_query(user_sql)
            
            if not user_rows or len(user_rows) == 0:
                raise ValueError(f"Критическая ошибка: Базовый аккаунт {look_num} не привязан ни к одному пользователю.")
                
            user_id = user_rows[0]['id']
            broker_id = 1 # Freedom Broker Казахстан всегда имеет ID = 1
            
            portfolio_id = None
            if account_type == "trade":
                p_sql = f"SELECT id FROM portfolios WHERE owner_id = {user_id} LIMIT 1"
                p_rows = self.db.execute_query(p_sql)
                if p_rows and len(p_rows) > 0:
                    portfolio_id = p_rows[0]['id']
                else:
                    p_insert = f"INSERT INTO portfolios (owner_id, name) VALUES ({user_id}, 'Основной')"
                    self.db.execute_query(p_insert)
                    p_rows_retry = self.db.execute_query(p_sql)
                    portfolio_id = p_rows_retry[0]['id'] if p_rows_retry else None

            # Вызываем "Китов" для создания базовой USD строки кошелька
            self.db.ensure_currency("USD")
            self.db.ensure_account_sub_row(user_id, portfolio_id, broker_id, account_number, account_type, "USD")
            
            acc_data = self.db.execute_query(acc_sql)

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
            sql_wallet_update = f"UPDATE accounts SET cash_available = {available}, cash_reserved = {reserved}, last_updated = CURRENT_TIMESTAMP WHERE account_number = '{account_number}' AND currency_id = '{currency}'"
            self.db.execute_query(sql_wallet_update)

        # --- ЭТАП Б: ОБНОВЛЕНИЕ ЦЕННЫХ БУМАГ (ЧЕРЕЗ КИТОВ) ---
        synced_assets_count = 0
        if account_type == "trade" and portfolio_id:
            active_ticker_ids = []
            total_market_value = 0

            for pos in positions:
                full_ticker = pos.get("i")
                quantity = float(pos.get("q", 0))
                avg_price = float(pos.get("price_a", 0))
                currency = pos.get("curr", "USD")
                market_val = float(pos.get("market_value", 0))

                if not full_ticker or quantity <= 0:
                    continue

                total_market_value += market_val
                
                # Кит №1 и №3: Гарантируем наличие валюты и тикера до начала операций
                self.db.ensure_currency(currency)
                self.db.ensure_ticker(full_ticker, currency)
                
                # Извлекаем ID тикера (он теперь гарантированно существует)
                t_res = self.db.execute_query(f"SELECT id FROM tickers WHERE full_ticker = '{full_ticker}'")
                ticker_id = int(t_res[0]['id'])
                active_ticker_ids.append(ticker_id)

                asset_search = f"SELECT id FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id = {ticker_id}"
                asset_res = self.db.execute_query(asset_search)

                if asset_res and len(asset_res) > 0:
                    sql_asset_update = f"UPDATE assets SET quantity = {quantity}, avg_price = {avg_price}, currency_id = '{currency}', last_updated = CURRENT_TIMESTAMP WHERE id = {asset_res[0]['id']}"
                    self.db.execute_query(sql_asset_update)
                else:
                    sql_asset_insert = f"INSERT INTO assets (portfolio_id, ticker_id, quantity, avg_price, currency_id) VALUES ({portfolio_id}, {ticker_id}, {quantity}, {avg_price}, '{currency}') ON CONFLICT (portfolio_id, ticker_id) DO NOTHING;"
                    self.db.execute_query(sql_asset_insert)

            if active_ticker_ids:
                ids_str = ",".join(map(str, active_ticker_ids))
                self.db.execute_query(f"DELETE FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id NOT IN ({ids_str})")
            else:
                self.db.execute_query(f"DELETE FROM assets WHERE portfolio_id = {portfolio_id}")

            self.db.execute_query(f"UPDATE accounts SET assets_value = {total_market_value} WHERE account_number = '{account_number}' AND currency_id = 'USD'")
            synced_assets_count = len(active_ticker_ids)

        # --- ЭТАП В: СИНХРОНИЗАЦИЯ ОРДЕРОВ (ЧЕРЕЗ КИТОВ ПЕРЕД ТРАНЗАКЦИЕЙ) ---
        try:
            # Вызываем метод только для торговых счетов (на D-счетах ордеров на акции нет)
            if portfolio_id and account_type == "trade":
                active_orders_list = fb_client.get_active_orders()
                
                # ЕДИНЫЙ ПРОТОКОЛ: Прогоняем каждый ордер через трех Китов ДО открытия транзакции
                for order in active_orders_list:
                    self.db.ensure_currency(order['currency_id'])
                    self.db.ensure_account_sub_row(user_id, portfolio_id, broker_id, account_number, account_type, order['currency_id'])
                    self.db.ensure_ticker(order['ticker'], order['currency_id'])
                
                # Все сущности на месте. Вызываем чистую и размеренную вставку ордеров
                from database import Database
                Database.sync_portfolio_orders(
                    portfolio_id=portfolio_id, 
                    account_number=account_number, 
                    api_orders=active_orders_list
                )
        except Exception as o_err:
            print(f"⚠️ Предупреждение: Не удалось обновить слепок приказов: {o_err}")

        return {
            "owner_name": owner_name,
            "account_number": account_number,
            "account_type": account_type,
            "synced_assets": synced_assets_count,
            "mode": logging_mode
        }
