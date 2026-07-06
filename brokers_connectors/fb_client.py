import hmac
import hashlib
import json
import time
import requests
import os

class FreedomBrokerClient:
    """Транспортный ('глупый') клиент к API Freedom Broker (Tradernet v2, Казахстан)."""
    
    def __init__(self, public_key: str, private_key: str):
        self.public_key = public_key
        self.private_key = private_key
        # Безопасная склейка URL
        self.host = f"{'https'}://{'tradernet.kz'}/{'api'}/"

    @classmethod
    def create_for_user(cls, user_id: int, db_instance) -> "FreedomBrokerClient":
        """
        🏭 Унифицированная фабрика для автоматического создания клиента по user_id.
        Сам ходит в базу за префиксом и вытягивает ключи из загруженного .env.
        """
        print(f"🏭 [FB CLIENT FACTORY]: Сборка клиента для пользователя ID: {user_id}...")
        
        # 1. Запрашиваем префикс пользователя из базы данных через f-строку (безопасно для int)
        sql = f"SELECT prefix FROM public.users WHERE id = {int(user_id)} LIMIT 1;"
        user_rows = db_instance.execute_query(sql)
        
        # Проверяем, что база вернула непустой список
        if not user_rows or len(user_rows) == 0:
            print(f"⚠️ [FACTORY WARNING]: Пользователь с ID {user_id} не найден в БД.")
            return None
            
        # Извлекаем первую строку-словарь из полученного списка
        user_row = user_rows[0]
        
        prefix = user_row.get('prefix')
        if not prefix:
            print(f"⚠️ [FACTORY WARNING]: У пользователя ID {user_id} отсутствует текстовый prefix.")
            return None
            
        prefix_upper = prefix.upper().strip()
        
        # 2. Формируем имена переменных и забираем ключи из памяти процесса (.env)
        api_key_env = f"FB_{prefix_upper}_API_KEY"
        api_secret_env = f"FB_{prefix_upper}_API_SECRET"
        
        public_key = os.getenv(api_key_env)
        private_key = os.getenv(api_secret_env)
        
        if not public_key or not private_key:
            print(f"⚠️ [FACTORY WARNING]: В .env не найдены ключи {api_key_env} или {api_secret_env}.")
            return None
            
        print(f"✅ [FACTORY SUCCESS]: Клиент для префикса {prefix_upper} успешно собран.")
        return cls(public_key=public_key, private_key=private_key)

    def execute(self, command: str, params: dict = None) -> dict:
        """Базовый универсальный метод для отправки любой команды к API."""
        if params is None:
            params = {}
            
        timestamp = str(int(time.time()))
        payload_str = json.dumps(params, separators=(',', ':'))
        string_to_sign = payload_str + timestamp
        
        signature = hmac.new(
            self.private_key.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "Content-Type": "application/json",
            "X-NtApi-PublicKey": self.public_key,
            "X-NtApi-Timestamp": timestamp,
            "X-NtApi-Sig": signature
        }
        
        full_url = f"{self.host}{command}"
        
        try:
            response = requests.post(full_url, data=payload_str, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Сбой сети API Freedom Broker: {e}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Некорректный JSON от сервера. Ответ: {response.text[:200]}")

    def get_active_orders(self) -> list:
        """
        Запрашивает у Freedom Broker актуальные приказы.
        Глубоко анализирует типы (лимиты, стопы, тейки) и направления (buy/sell).
        """
        params = {
            "active_only": 1  # Только живые активные приказы
        }

        raw_response = self.execute(command="getNotifyOrderJson", params=params)
        
        response_data = []
        if isinstance(raw_response, dict):
            result_node = raw_response.get("result", {})
            orders_node = result_node.get("orders", {}) if isinstance(result_node, dict) else {}
            if isinstance(orders_node, dict):
                response_data = orders_node.get("order", [])

        if not response_data or not isinstance(response_data, list):
            return []

        processed_orders = []
        for order in response_data:
            # 1. Точное определение направления операции (oper: 1 - buy, 3 - sell)
            raw_action = order.get('oper')
            if raw_action == 1:
                action_str = 'buy'
            elif raw_action == 3:
                action_str = 'sell'
            else:
                action_str = 'buy' if raw_action == 2 else 'sell' # Резервный маппинг v1
                
            # 2. Глубокий анализ типа ордера (Лимит, Стоп-лосс, Тейк-профит)
            raw_type = order.get('type')
            stop_price = float(order.get('stop_init_price', 0))
            
            if raw_type == 6:
                order_type_str = 'take_profit'
            elif raw_type in (3, 5) or stop_price > 0:
                # Если тип указывает на стоп или вшита стоп-цена контроля рисков — это стоп-лосс
                order_type_str = 'stop_loss'
            elif raw_type == 4:
                order_type_str = 'stop_limit'
            elif raw_type == 1:
                order_type_str = 'market'
            else:
                order_type_str = 'limit'

            processed_order = {
                "broker_order_id": str(order.get('order_id') or order.get('id')),
                "ticker": order.get('instr'),
                "action": action_str,
                "order_type": order_type_str,
                "status": "active",
                "oper": int(raw_action) if raw_action is not None else 1, # Чистый код операции (1, 2, 3)
                "type": int(raw_type) if raw_type is not None else 2,     # Чистый код типа (2 - лимит)
                "q": float(order.get('q', 0)),
                "p": float(order.get('p', 0)),
                "stop_init_price": float(order.get('stop_init_price', 0)),
                "currency_id": order.get('cur', 'USD').upper()
            }
            processed_orders.append(processed_order)

        return processed_orders

    def get_security_info(self, ticker: str) -> dict:
        """
        Интерпретатор 'Туда': Запрашивает у брокера спецификацию тикера по СУП.
        Позволяет узнать default_ticker (международное имя для Yahoo).
        """
        params = {
            "ticker": ticker,
            "sup": True
        }
        raw = self.execute(command="getSecurityInfo", params=params)
        
        # Безопасно вытаскиваем тело ответа брокера
        if isinstance(raw, dict) and "result" in raw:
            return raw["result"]
        return raw if isinstance(raw, dict) else {}

    def find_ticker(self, text_phrase: str) -> list:
        """Интерпретатор 'Обратно' v4.0. Ищет тикер по базе брокера (tickerFinder). Отдаёт СПИСОК совпадений json-ом. Для выявления конкретной бумаги список нужно фильтовать по тикеру и рынку"""
        params = {"text": str(text_phrase).strip()}
        # 🔥 ИСПРАВЛЕНО ДЛЯ АНАЛИТИКИ: Убран ошибочный узел result, из-за которого ломался поиск широкого рынка
        raw = self.execute(command="tickerFinder", params=params)
        if isinstance(raw, dict):
            if "found" in raw:
                return raw["found"]
            elif "result" in raw and isinstance(raw["result"], dict):
                return raw["result"].get("found", [])
        return []

    def get_active_alerts(self, ticker: str = None) -> list:
        """
        📡 ИНСТРУМЕНТ 3: Запрос списка текущих ценовых алертов из Freedom Broker API.
        Метод реализует вызов команды 'getAlertsList' на базе внутреннего шлюза self.execute.
        """
        print(f"📡 [FB CLIENT API]: Запрашиваю список алертов у брокера для тикера: {ticker or 'ВСЕ'}")
        
        # Собираем параметры согласно вашей спецификации и ТЗ
        params = {}
        if ticker:
            params["ticker"] = str(ticker).upper().strip()
            
        try:
            # Вызываем команду через ваш родной метод execute
            raw_response = self.execute(command="getAlertsList", params=params)
            
            # Проверяем общие критические ошибки API из документации
            if isinstance(raw_response, dict) and "code" in raw_response and "errMsg" in raw_response:
                print(f"🚨 [API ALERTS ERROR]: Сбой метода ФБ! Код {raw_response['code']}: {raw_response['errMsg']}")
                return []
                
            # Извлекаем массив алертов
            alerts_list = []
            if isinstance(raw_response, dict):
                # В зависимости от шлюза ФБ, массив может лежать в корне или внутри узла result
                alerts_list = raw_response.get("alerts", []) or raw_response.get("result", {}).get("alerts", [])
            
            # Страхуемся от точечных ошибок по конкретным инструментам ("Instrument not found")
            if isinstance(alerts_list, list) and len(alerts_list) > 0:
                if isinstance(alerts_list, dict) and "error" in alerts_list:
                    print(f"⚠️ [API ALERTS WARNING]: Брокер вернул ошибку: {alerts_list['error']}")
                    return []
                    
            return alerts_list if isinstance(alerts_list, list) else []
            
        except Exception as api_err:
            print(f"❌ [FB CLIENT CRITICAL EXCEPTION]: Не удалось забрать алерты по API: {api_err}")
            return []

    def get_quotes(self, tickers) -> dict | float:
        """
        📊 УНИВЕРСАЛЬНЫЙ ИНСТРУМЕНТ: Пакетный и одиночный REST-сборщик котировок.
        Принимает как одну строку (тикер), так и список строк (пачку тикеров).
        Автоматически зачищает текстовые JSON-запятые брокера и выдает чистые float.
        """
        # 1. Унифицируем входной параметр: превращаем одиночную строку в список из одного элемента
        is_single = isinstance(tickers, str)
        tickers_list = [tickers.strip().upper()] if is_single else [str(t).strip().upper() for t in tickers]
        
        if not tickers_list:
            return 0.0 if is_single else {}

        # 2. Отправляем пакетный запрос к брокеру через наш универсальный транспорт execute
        try:
            raw_response = self.execute(command="getStockQuotesJson", params={"tickers": tickers_list})
            
            if isinstance(raw_response, dict) and "code" in raw_response and "errMsg" in raw_response:
                print(f"🚨 [API QUOTES ERROR]: Сбой метода ФБ! Код {raw_response['code']}: {raw_response['errMsg']}")
                return 0.0 if is_single else {}
                
            result_node = raw_response.get("result", {}) if isinstance(raw_response, dict) else {}
            result_array = result_node.get("q", []) if isinstance(result_node, dict) else []
            
            if not result_array or not isinstance(result_array, list):
                return 0.0 if is_single else {}
                
            # 3. Собираем результаты в чистый словарь Python с валидацией типов данных
            parsed_quotes = {}
            for quote in result_array:
                ticker_name = quote.get("c")
                if not ticker_name:
                    continue
                    
                ticker_name = str(ticker_name).upper().strip()
                raw_price = quote.get("ltp")
                
                if raw_price is not None and str(raw_price) != 'nan' and str(raw_price).strip() != '':
                    try:
                        # Физически истребляем десятичные запятые брокера, переводя в стерильный float
                        clean_price = float(str(raw_price).replace(',', '.').strip())
                        parsed_quotes[ticker_name] = clean_price
                    except (ValueError, TypeError):
                        parsed_quotes[ticker_name] = 0.0
                else:
                    parsed_quotes[ticker_name] = 0.0

            # 4. Умный возврат в зависимости от того, как метод был вызван
            if is_single:
                # Возвращаем просто число float для одиночной бумаги (нужно для ядра базы)
                target_ticker = tickers_list[0]
                return parsed_quotes.get(target_ticker, 0.0)
            
            # Возвращаем весь словарь {тикер: float_цена} для пакетного обновителя
            return parsed_quotes

        except Exception as e:
            print(f"❌ [FB CLIENT CRITICAL EXCEPTION] Сбой метода get_quotes: {e}")
            return 0.0 if is_single else {}
