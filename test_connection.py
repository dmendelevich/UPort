import os
from dotenv import load_dotenv
from pathlib import Path

# Импортируем компоненты из разных слоев архитектуры
from brokers_connectors.fb_client import FreedomBrokerClient
from repositories.portfolio_repository import PortfolioRepository

def main():
    print("=== [UPort] Умный тест архитектуры (Транспорт -> Репозиторий) ===")

    # 1. Загрузка переменных окружения
    env_path = Path('/root/UPort/.env')
    load_dotenv(dotenv_path=env_path)
    
    api_key = os.getenv("FB_DLM_API_KEY")
    api_secret = os.getenv("FB_DLM_API_SECRET")

    if not api_key or not api_secret:
        print("❌ ERROR: Ключи для FB_DLM_API_KEY/SECRET не найдены в .env.")
        return

    print("✅ Переменные окружения успешно считаны.")

    # 2. Инициализация низкоуровневого ("глупого") клиента
    print("🤖 Инициализация инфраструктурного FreedomBrokerClient (Слой 1)...")
    fb_transport = FreedomBrokerClient(public_key=api_key, private_key=api_secret)

    # 3. Инициализация высокоуровневого репозитория
    print("🧠 Инициализация PortfolioRepository и передача коннектора (Слой 2)...")
    portfolio_repo = PortfolioRepository(fb_client=fb_transport)

    # 4. Запрос очищенных данных через репозиторий
    print("\n📡 Запрос стандартизированного портфеля через Репозиторий...")
    try:
        # Репозиторий сам сходил в коннектор, забрал JSON, распарсил его и вернул стандарт
        clean_portfolio = portfolio_repo.get_fb_portfolio_clean()
        
        print("\n🏆 РЕЗУЛЬТАТ ИЗ РЕПОЗИТОРИЯ (Компоненты UPort работают корректно):")
        if not clean_portfolio:
            print("   Портфель пуст.")
        for item in clean_portfolio:
            print(f"   • {item['ticker']}: {item['quantity']} шт.")
            
    except Exception as e:
        print(f"❌ Сбой на уровне Репозитория: {e}")

    print("\n=== Тестирование новой архитектуры завершено ===")

if __name__ == "__main__":
    main()
