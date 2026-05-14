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
