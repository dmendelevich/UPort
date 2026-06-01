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
            sql_wallet_update = f"UPDATE accounts SET cash_available = {available}, cash_reserved = {reserved}, last_updated = transaction_timestamp() WHERE account_number = '{account_number}' AND currency_id = '{currency}'"
            self.db.execute_query(sql_wallet_update)

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
                
                # Запускаем Кит v3.0 с СУП-переводчиком имен Freedom Broker
                ticker_id, listing_id = self.db.ensure_ticker_v2(
                    broker_id=1, 
                    broker_symbol=full_ticker, 
                    fallback_currency=currency,
                    fb_client=fb_client
                )
                
                # Фиксируем интерес бумаги в watchlist со статусом 'bought'
                self.db.ensure_watchlist_row(portfolio_id, listing_id, source_type="user")
                self.db.execute_query(f"UPDATE public.watchlist SET status = 'bought'::public.ticker_lifecycle_status WHERE portfolio_id = {portfolio_id} AND listing_id = {listing_id};")
                
                active_listing_ids.append(listing_id)

                # Проверяем, есть ли уже этот листинг в портфеле assets
                asset_search = f"SELECT id FROM assets WHERE portfolio_id = {portfolio_id} AND listing_id = {listing_id}"
                asset_res = self.db.execute_query(asset_search)

                if asset_res and len(asset_res) > 0:
                    sql_asset_update = f"UPDATE assets SET quantity = {quantity}, avg_price = {avg_price}, last_updated = '{session_start_time}' WHERE id = {asset_res[0]['id']}"
                    self.db.execute_query(sql_asset_update)
                else:
                    # 🔥 ЗАПИРАЕМ ХОЛДИНГ-ДНИ: При первой записи жестко фиксируем position_opened_at
                    sql_asset_insert = f"""
                        INSERT INTO assets (portfolio_id, listing_id, quantity, avg_price, last_updated, position_opened_at) 
                        VALUES ({portfolio_id}, {listing_id}, {quantity}, {avg_price}, '{session_start_time}', '{session_start_time}') 
                        ON CONFLICT (portfolio_id, listing_id) 
                        DO UPDATE SET 
                            quantity = EXCLUDED.quantity, 
                            avg_price = EXCLUDED.avg_price, 
                            last_updated = '{session_start_time}'; 
                    """
                    self.db.execute_query(sql_asset_insert)
                    
            # ИНТЕЛЛЕКТУАЛЬНАЯ БОРЬБА С ФАНТОМАМИ (КРУГОВОРОТ В WATCHLIST)
            # Находим листинги, которые пропали из ответа брокера (проданы в ноль)
            sql_find_phantoms = f"SELECT listing_id FROM assets WHERE portfolio_id = {portfolio_id} AND (last_updated < '{session_start_time}' OR last_updated IS NULL)"
            phantom_rows = self.db.execute_query(sql_find_phantoms)
            
            if phantom_rows and isinstance(phantom_rows, list):
                for p_row in phantom_rows:
                    # 🔥 ЗАЩИТА: Пропускаем legacy-строки, у которых еще нет ID листинга
                    if p_row.get('listing_id') is None:
                        continue 
                        
                    ph_id = int(p_row['listing_id'])
                    # Переводим бумагу инвестора в статус исторического контекста 'sold_out'
                    print(f"♻️ [Sync Manager]: Листинг #{ph_id} продан в ноль. Перевожу в статус 'sold_out'...")
                    self.db.execute_query(f"UPDATE public.watchlist SET status = 'sold_out'::public.ticker_lifecycle_status, updated_at = CURRENT_TIMESTAMP WHERE portfolio_id = {portfolio_id} AND listing_id = {ph_id};")

            # 🔥 ФИКС ФАНТОМОВ v3.3: Исключаем живые, только что обновленные листинги из удаления!
            if active_listing_ids:
                # Превращаем список [1, 2, 3] в строку "1,2,3" для SQL-запроса
                active_ids_str = ",".join(map(str, active_listing_ids))
                
                sql_cleanup = f"""
                    DELETE FROM assets 
                    WHERE portfolio_id = {portfolio_id} 
                      AND listing_id NOT IN ({active_ids_str});
                """
                self.db.execute_query(sql_cleanup)


            # Обновляем агрегированную долларовую стоимость активов счета в accounts
            self.db.execute_query(f"UPDATE accounts SET assets_value = {total_market_value} WHERE account_number = '{account_number}' AND currency_id = 'USD'")
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
                    
                    # Гарантируем регистрацию листинга ордера через наш СУП-переводчик имен
                    ord_ticker_id, ord_listing_id = self.db.ensure_ticker_v2(
                        broker_id=1, 
                        broker_symbol=order['ticker'], 
                        fallback_currency=order['currency_id'],
                        fb_client=fb_client
                    )
                    
                    # Переводим листинг ордера в статус 'ordered', если он до этого просто изучался
                    self.db.ensure_watchlist_row(portfolio_id, ord_listing_id, source_type="user")
                    self.db.execute_query(f"UPDATE public.watchlist SET status = 'ordered'::public.ticker_lifecycle_status WHERE portfolio_id = {portfolio_id} AND listing_id = {ord_listing_id} AND status = 'considered'::public.ticker_lifecycle_status;")
                
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
