import os
import sys
from dotenv import load_dotenv
from pathlib import Path

# Подгружаем ваше окружение
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

# Добавляем корень в пути импорта Python, чтобы он видел папку connectors
sys.path.append(os.getcwd())

# Импортируем ваш текущий рабочий класс клиента
from brokers_connectors.fb_client import FreedomBrokerClient

def run_test():
    print("🚀 [Тестер]: Инициализация тестового запроса котировок...")
    
    # Считываем ваши ключи отца (DLM)
    api_key = os.getenv("FB_DLM_API_KEY")
    api_secret = os.getenv("FB_DLM_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ Ошибка: В .env файле не найдены ключи FB_DLM_API_KEY / SECRET.")
        return

    # Создаем экземпляр вашего транспортного клиента
    client = FreedomBrokerClient(public_key=api_key, private_key=api_secret)
    
    # Железно прописываем массив из трех акций
    test_tickers = ["AAPL.US", "AMZN.US"]
    
    print(f"📡 [Тестер]: Отправка POST-запроса getStockQuotesJson для {test_tickers}...")
    
    try:
        # Вызываем ваш базовый метод execute строго по нашему согласованному плану
        raw_response = client.execute(
            command="getStockQuotesJson", 
            params={"tickers": test_tickers}
        )
        
        print("\n🏆 [УСПЕХ]: Брокер вернул ответ!")
        print("--------------------------------------------------")
        print(raw_response)
        print("--------------------------------------------------\n")
        
    except Exception as e:
        print(f"\n❌ [ОШИБКА СВЯЗИ]: Запрос заблокирован или упал: {e}\n")

if __name__ == "__main__":
    run_test()
