import json
import logging
from database import Database

# Отключаем лишние логи, чтобы видеть только чистый результат
logging.basicConfig(level=logging.WARNING)

def run_test():
    print("🧪 --- СТАРТ ТЕСТИРОВАНИЯ МЕТОДА КОНТЕКСТА ТИКЕРА --- 🧪\n")
    
    # Инициализируем подключение через ваш шлюз
    db = Database(role="BOT")
    
    # ПОДСТАВЬ СВОЙ РЕАЛЬНЫЙ TELEGRAM ID ДЛЯ ТЕСТА ПЕРСОНАЛЬНОГО РЕЖИМА
    # (Тот самый ID, который внесен в таблицу users для твоего профиля)
    MY_TELEGRAM_ID = 250720161  # <--- Замени это число на свой реальный ID
    
    ticker_to_test = "GLDM.US"

    # 1. ТЕСТ ГЛОБАЛЬНОГО РЕЖИМА (Взгляд ИИ / Общий семейный капитал)
    print(f"🔍 1. Запрос ГЛОБАЛЬНОГО контекста для тикера: {ticker_to_test}...")
    global_res = db.get_ticker_context(ticker_to_test)
    print(json.dumps(global_res, indent=4, ensure_ascii=False))
    print("\n" + "="*50 + "\n")

    # 2. ТЕСТ ПЕРСОНАЛЬНОГО РЕЖИМА (Взгляд обычного пользователя)
    print(f"👤 2. Запрос ПЕРСОНАЛЬНОГО контекста для твоего Telegram ID ({MY_TELEGRAM_ID})...")
    personal_res = db.get_ticker_context(ticker_to_test, telegram_id=MY_TELEGRAM_ID)
    print(json.dumps(personal_res, indent=4, ensure_ascii=False))

if __name__ == "__main__":
    run_test()
