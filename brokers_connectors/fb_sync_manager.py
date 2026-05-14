import os
import json

class FreedomBrokerSyncManager:
    """Менеджер для глубокой синхронизации активов и мультивалютных D-счетов Freedom Broker."""

    def __init__(self, db_instance, fb_client_class):
        self.db = db_instance
        self.fb_client_class = fb_client_class

    def _get_or_create_ticker_id(self, full_ticker: str, currency_id: str) -> int:
        """Ищет тикер в БД или создает новый."""
        sql_search = f"SELECT id FROM tickers WHERE full_ticker = '{full_ticker}'"
        res = self.db.execute_query(sql_search)
        if res and isinstance(res, list) and len(res) > 0:
            return int(res[0]['id'])

        symbol, suffix = full_ticker.split(".", 1) if "." in full_ticker else (full_ticker, "US")
        sql_insert = f"""
        INSERT INTO tickers (symbol, suffix, full_ticker, currency_id)
        VALUES ('{symbol}', '{suffix}', '{full_ticker}', '{currency_id}')
        """
        self.db.execute_query(sql_insert)
        
        res_retry = self.db.execute_query(sql_search)
        return int(res_retry[0]['id'])

    def sync_by_account_number(self, account_number: str) -> dict:
        """Синхронизирует активы и кэш, динамически выбирая торговые или накопительные ключи."""
        
        # 1. Находим параметры счета в таблице accounts
        acc_sql = f"""
            SELECT a.user_id, a.portfolio_id, a.account_type, u.name as owner_name, u.prefix 
            FROM accounts a
            JOIN users u ON a.user_id = u.id
            WHERE a.account_number = '{account_number}' LIMIT 1
        """
        acc_data = self.db.execute_query(acc_sql)
        if not acc_data:
            raise ValueError(f"Счет {account_number} не зарегистрирован в таблице accounts.")
        
        user_id = acc_data[0]['user_id']
        portfolio_id = acc_data[0]['portfolio_id']
        account_type = acc_data[0]['account_type']
        owner_name = acc_data[0]['owner_name']
        prefix = acc_data[0]['prefix']

        # 2. Выбор правильной пары ключей в зависимости от типа счета (trade или deposit)
        if account_type == "deposit":
            # Используем новые ключи накопительного D-счета
            api_key = os.getenv(f"FB_{prefix}_D_API_KEY")
            api_secret = os.getenv(f"FB_{prefix}_D_API_SECRET")
            logging_mode = "НАКОПИТЕЛЬНЫЙ (D-ключи)"
        else:
            # Используем стандартные торговые ключи
            api_key = os.getenv(f"FB_{prefix}_API_KEY")
            api_secret = os.getenv(f"FB_{prefix}_API_SECRET")
            logging_mode = "ТОРГОВЫЙ (Т-ключи)"

        if not api_key or not api_secret:
            raise ValueError(f"Критическая ошибка: Ключи для режима {logging_mode} не найдены в .env.")

        # Инициализируем транспортный клиент с выбранными ключами
        fb_client = self.fb_client_class(public_key=api_key, private_key=api_secret)

        # 3. Запрашиваем живые данные по API
        raw_res = fb_client.execute("getPositionJson", params={})
        if isinstance(raw_res, dict) and "error" in raw_res:
            raise RuntimeError(f"Ошибка API Freedom Broker ({logging_mode}): {raw_res['error']}")
            
        result_node = raw_res.get("result", {})
        ps_node = result_node.get("ps", {})
        
        positions = ps_node.get("pos", [])
        cash_balances = ps_node.get("acc", [])

        # --- ЭТАП А: ОБНОВЛЕНИЕ МУЛЬТИВАЛЮТНОГО КЭША (Торговый или D-счет) ---
        for cash in cash_balances:
            currency = cash.get("curr", "USD")
            available = float(cash.get("s", 0))
            reserved = abs(float(cash.get("t2_out", 0))) 

            # Проверяем наличие конкретного кошелька (номер счета + валюта)
            check_wallet = f"SELECT id FROM accounts WHERE account_number = '{account_number}' AND currency_id = '{currency}'"
            wallet_res = self.db.execute_query(check_wallet)

            if wallet_res:
                sql_wallet_update = f"""
                UPDATE accounts 
                SET cash_available = {available}, cash_reserved = {reserved}, last_updated = CURRENT_TIMESTAMP
                WHERE id = {wallet_res[0]['id']}
                """
                self.db.execute_query(sql_wallet_update)
            else:
                # Если валюта (например, KZT или EUR) пришла впервые — автоматически создаем строку
                p_id_val = portfolio_id if portfolio_id else "NULL"
                sql_wallet_insert = f"""
                INSERT INTO accounts (user_id, portfolio_id, account_number, account_type, currency_id, cash_available, cash_reserved)
                VALUES ({user_id}, {p_id_val}, '{account_number}', '{account_type}', '{currency}', {available}, {reserved})
                """
                self.db.execute_query(sql_wallet_insert)

        # --- ЭТАП Б: ОБНОВЛЕНИЕ ЦЕННЫХ БУМАГ (Только для торговых счетов) ---
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
                ticker_id = self._get_or_create_ticker_id(full_ticker, currency)
                active_ticker_ids.append(ticker_id)

                asset_search = f"SELECT id FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id = {ticker_id}"
                asset_res = self.db.execute_query(asset_search)

                if asset_res:
                    sql_asset_update = f"""
                    UPDATE assets 
                    SET quantity = {quantity}, avg_price = {avg_price}, currency_id = '{currency}', last_updated = CURRENT_TIMESTAMP
                    WHERE id = {asset_res[0]['id']}
                    """
                    self.db.execute_query(sql_asset_update)
                else:
                    sql_asset_insert = f"""
                    INSERT INTO assets (portfolio_id, ticker_id, quantity, avg_price, currency_id)
                    VALUES ({portfolio_id}, {ticker_id}, {quantity}, {avg_price}, '{currency}')
                    """
                    self.db.execute_query(sql_asset_insert)

            if active_ticker_ids:
                ids_str = ",".join(map(str, active_ticker_ids))
                self.db.execute_query(f"DELETE FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id NOT IN ({ids_str})")
            else:
                self.db.execute_query(f"DELETE FROM assets WHERE portfolio_id = {portfolio_id}")

            self.db.execute_query(f"UPDATE accounts SET assets_value = {total_market_value} WHERE account_number = '{account_number}' AND currency_id = 'USD'")
            synced_assets_count = len(active_ticker_ids)

        return {
            "owner_name": owner_name,
            "account_number": account_number,
            "account_type": account_type,
            "synced_assets": synced_assets_count,
            "mode": logging_mode
        }
