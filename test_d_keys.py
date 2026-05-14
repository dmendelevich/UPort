from database import Database
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.fb_sync_manager import FreedomBrokerSyncManager

def main():
    print("--- [ТЕСТ] Верификация новых D-ключей для накопительного счета ---")
    db_sys = Database(role="SYSTEM")
    manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

    d_account = "D7A17092"
    try:
        res = manager.sync_by_account_number(d_account)
        print("\n🏆 ТЕСТ ЗАВЕРШЕН УСПЕШНО:")
        print(f" 👤 Владелец: {res['owner_name']}")
        print(f" 💳 Счет: {res['account_number']} ({res['account_type']})")
        print(f" 🔒 Использован режим: {res['mode']}")
        print("✅ Мультивалютные остатки D-счета успешно импортированы в базу!")
    except Exception as e:
        print(f"❌ Ошибка тестирования D-ключей: {e}")

if __name__ == "__main__":
    main()
