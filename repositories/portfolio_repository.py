import os
import json

class PortfolioRepository:
    """Репозиторий для глубокой синхронизации активов из API брокеров в PostgreSQL."""

    def __init__(self, db_instance, fb_client_class):
        # db_instance — это экземпляр класса Database(role="SYSTEM")
        self.db = db_instance
        # Передаем сам класс клиента, чтобы создавать его экземпляры динамически
        self.fb_client_class = fb_client_class

    def _get_or_create_ticker_id(self, full_ticker: str, currency_id: str) -> int:
        """Проверяет тикер в БД. Если его нет — создает."""
        sql_search = f"SELECT id FROM tickers WHERE full_ticker = '{full_ticker}'"
        res = self.db.execute_query(sql_search)
        if res and isinstance(res, list) and len(res) > 0:
            return int(res[0]['id'])

        if "." in full_ticker:
            symbol, suffix = full_ticker.split(".", 1)
        else:
            symbol, suffix = full_ticker, "US"

        sql_insert = f"""
        INSERT INTO tickers (symbol, suffix, full_ticker, currency_id)
        VALUES ('{symbol}', '{suffix}', '{full_ticker}', '{currency_id}')
        """
        self.db.execute_query(sql_insert)
        
        # Забираем созданный ID
        res_retry = self.db.execute_query(sql_search)
        return int(res_retry[0]['id'])

    def sync_portfolio_by_tg_id(self, telegram_id: int) -> dict:
        """Находит пользователя и его портфели, опрашивает API брокера и обновляет assets."""
        
        # 1. Ищем пользователя и его префикс в базе данных
        user_sql = f"SELECT id, name, prefix FROM users WHERE telegram_id = {telegram_id}"
        user_data = self.db.execute_query(user_sql)
        if not user_data:
            raise ValueError(f"Пользователь с Telegram ID {telegram_id} не найден в БД UPort.")
        
        owner_id = user_data[0]['id']
        owner_name = user_data[0]['name']
        prefix = user_data[0]['prefix']

        if not prefix:
            raise ValueError(f"Для пользователя {owner_name} не настроен 'prefix' в таблице users.")

        # 2. Динамически считываем ключи из .env для этого префикса
        api_key = os.getenv(f"FB_{prefix}_API_KEY")
        api_secret = os.getenv(f"FB_{prefix}_API_SECRET")

        if not api_key or not api_secret:
            raise ValueError(f"Ключи FB_{prefix}_API_KEY/SECRET не найдены в файле .env.")

        # Инициализируем транспортный коннектор с живыми ключами
        fb_client = self.fb_client_class(public_key=api_key, private_key=api_secret)

        # 3. Находим все портфели этого владельца в БД
        portfolios_sql = f"SELECT id, name FROM portfolios WHERE owner_id = {owner_id}"
        portfolios_data = self.db.execute_query(portfolios_sql)
        if not portfolios_data:
            raise ValueError(f"В БД не найдено ни одного портфеля для пользователя {owner_name}.")

        report_details = []

        # 4. В цикле синхронизируем каждый портфель (для теста — П10)
        for p in portfolios_data:
            portfolio_id = p['id']
            portfolio_name = p['name']

            # Опрашиваем Freedom Broker по API
            raw_res = fb_client.execute("getPositionJson", params={})
            if isinstance(raw_res, dict) and "error" in raw_res:
                raise RuntimeError(f"Ошибка API Freedom Broker: {raw_res['error']}")
                
            positions = raw_res.get("result", {}).get("ps", {}).get("pos", [])
            
            active_ticker_ids = []

            # Проходим по каждой живой бумаге из ответа брокера
            for pos in positions:
                full_ticker = pos.get("i")
                quantity = float(pos.get("q", 0))
                avg_price = float(pos.get("price_a", 0))
                currency = pos.get("curr", "USD")

                if not full_ticker or quantity <= 0:
                    continue

                # Получаем или создаем ID тикера в базе
                ticker_id = self._get_or_create_ticker_id(full_ticker, currency)
                active_ticker_ids.append(ticker_id)

                # Проверяем, есть ли уже этот актив в портфеле в assets
                asset_search = f"SELECT id FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id = {ticker_id}"
                asset_res = self.db.execute_query(asset_search)

                if asset_res:
                    # ГЛУБОКИЙ UPDATE: обновляем количество, цену закупки и валюту
                    asset_id = asset_res[0]['id']
                    sql_update = f"""
                    UPDATE assets 
                    SET quantity = {quantity}, avg_price = {avg_price}, currency_id = '{currency}', last_updated = CURRENT_TIMESTAMP
                    WHERE id = {asset_id}
                    """
                    self.db.execute_query(sql_update)
                else:
                    # INSERT нового актива в портфель
                    sql_insert = f"""
                    INSERT INTO assets (portfolio_id, ticker_id, quantity, avg_price, currency_id)
                    VALUES ({portfolio_id}, {ticker_id}, {quantity}, {avg_price}, '{currency}')
                    """
                    self.db.execute_query(sql_insert)

            # ОЧИСТКА: Удаляем из assets бумаги, которые были полностью проданы на бирже
            if active_ticker_ids:
                # Превращаем список ID в строку для SQL-условия NOT IN
                ids_str = ",".join(map(str, active_ticker_ids))
                sql_delete = f"DELETE FROM assets WHERE portfolio_id = {portfolio_id} AND ticker_id NOT IN ({ids_str})"
                self.db.execute_query(sql_delete)
            else:
                # Если на бирже вообще пусто, очищаем весь портфель в assets
                sql_delete = f"DELETE FROM assets WHERE portfolio_id = {portfolio_id}"
                self.db.execute_query(sql_delete)

            report_details.append({
                "portfolio_name": portfolio_name,
                "synced_count": len(active_ticker_ids)
            })

        return {
            "owner_name": owner_name,
            "results": report_details
        }
