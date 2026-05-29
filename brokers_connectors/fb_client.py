import hmac
import hashlib
import json
import time
import requests

class FreedomBrokerClient:
    """Транспортный ('глупый') клиент к API Freedom Broker (Tradernet v2, Казахстан)."""
    
    def __init__(self, public_key: str, private_key: str):
        self.public_key = public_key
        self.private_key = private_key
        # Безопасная склейка URL
        self.host = f"{'https'}://{'tradernet.kz'}/{'api'}/"

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
        """
        Интерпретатор 'Обратно': Ищет тикер по базе брокера (tickerFinder).
        Позволяет узнать nt_ticker (для сокетов), а также type и kind инструмента.
        """
        params = {
            "text": str(text_phrase).lower()
        }
        raw = self.execute(command="tickerFinder", params=params)
        
        if isinstance(raw, dict) and "result" in raw:
            res_node = raw["result"]
            if isinstance(res_node, dict):
                return res_node.get("found", [])
        return []


