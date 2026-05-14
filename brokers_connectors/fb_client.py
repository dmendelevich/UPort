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
