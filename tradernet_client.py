import hmac
import hashlib
import json
import time
import requests

class TradernetApiClient:
    """Клиент для работы с API Freedom Broker (Tradernet v2) для Казахстана."""
    
    def __init__(self, public_key: str, private_key: str):
        self.public_key = public_key
        self.private_key = private_key
        # Безопасная сборка URL без риска потери двоеточия
        self.host = f"{'https'}://{'tradernet.kz'}/{'api'}/"

    def request(self, command: str, params: dict = None) -> dict:
        """Отправка подписанного POST-запроса к API Freedom Broker."""
        if params is None:
            params = {}
            
        timestamp = str(int(time.time()))
        # Строгая сериализация JSON без лишних пробелов для точной подписи
        payload_str = json.dumps(params, separators=(',', ':'))
        string_to_sign = payload_str + timestamp
        
        # Генерация подписи HMAC-SHA256
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
            response.raise_for_status() # Вызовет исключение при HTTP ошибках (4xx, 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Сбой сети или HTTP при запросе к Freedom Broker: {e}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Сервер вернул некорректный JSON. Ответ сервера: {response.text[:200]}")
