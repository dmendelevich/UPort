import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Настраиваем пути, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent))

from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient

def run_factory_and_quotes_test():
    print("=== 🧪 [TEST] СТАРТ РАСШИРЕННОГО ТЕСТИРОВАНИЯ КЛИЕНТА FB ===\n")
    
    # Загружаем .env для изолированного запуска
    env_path = Path('/root/UPort/.env')
    load_dotenv(dotenv_path=env_path)
    
    # 1. Собираем боевой клиент для вашего профиля (ДЛМ, ID: 1)
    print("--- Шаг 1: Сборка клиента для ДЛМ (ID: 1) ---")
    client_dlm = FreedomBrokerClient.create_for_user(user_id=1, db_instance=db_sys)
    
    if not client_dlm:
        print("❌ Критическая ошибка: Не удалось создать клиента для ДЛМ. Тест прерван.")
        return
        
    print("🚀 Клиент успешно инициализирован. Переходим к тестированию котировок...\n")

    # ----------------------------------------------------
    # ТЕСТ А: Проверка одиночного вызова (Нужно для ядра СУБД)
    # ----------------------------------------------------
    print("--- ТЕСТ А: Одиночный запрос котировки (Вход: строка 'AAPL.US') ---")
    single_ticker = "AAPL.US"
    
    price = client_dlm.get_quotes(single_ticker)
    
    print(f"📥 Ответ метода get_quotes для '{single_ticker}': {price}")
    print(f"📊 Тип данных ответа: {type(price)}")
    if isinstance(price, float) and price > 0:
        print("✅ Успех: Метод вернул чистое число float! Запятые брокера успешно ликвидированы.\n")
    else:
        print("❌ Сбой: Метод вернул некорректное значение или неверный тип данных.\n")

    # ----------------------------------------------------
    # ТЕСТ Б: Проверка пакетного вызова (Нужно для обновителя цен)
    # ----------------------------------------------------
    print("--- ТЕСТ Б: Пакетный запрос котировок (Вход: список строк) ---")
    batch_tickers = ["AAPL.US", "AMD.US", "CIFR.US"]
    print(f"📡 Отправляю пачку тикеров брокеру: {batch_tickers}")
    
    quotes_dict = client_dlm.get_quotes(batch_tickers)
    
    print(f"📥 Ответ метода get_quotes (Получен словарь): {quotes_dict}")
    print(f"📊 Тип данных ответа: {type(quotes_dict)}")
    
    if isinstance(quotes_dict, dict) and len(quotes_dict) > 0:
        print("✅ Успех: Метод вернул валидный словарь с чистыми числовыми значениями!")
        # Проверяем типы данных внутри словаря
        all_floats = all(isinstance(v, float) for v in quotes_dict.values())
        if all_floats:
            print("⭐ Защита сработала: Абсолютно все цены внутри словаря приведены к типу float.\n")
        else:
            print("⚠️ Предупреждение: Внутри словаря обнаружены нечисловые типы данных.\n")
    else:
        print("❌ Сбой: Метод не смог вернуть заполненный словарь котировок.\n")

    print("=== 🏁 [TEST] ВСЕ ТЕСТЫ ДЛЯ МЕТОДА get_quotes ЗАВЕРШЕНЫ ===")

if __name__ == "__main__":
    run_factory_and_quotes_test()
