import requests
import json

def test_security():
    url = "http://localhost:3000/query"
    
    print("--- Старт верификации матрицы безопасности UPort AI Gateway ---\n")

    # Тестовые SQL запросы
    select_query = "SELECT id FROM assets LIMIT 1;"
    update_query = "UPDATE assets SET quantity = 10 WHERE id = 999999;"

    # 1. ТЕСТ: Роль SYSTEM (Фоновые процессы)
    print("🤖 Тестирование роли SYSTEM (Ожидаем УСПЕХ на UPDATE)...")
    headers_sys = {"X-Token": "uport_sys_secret"}
    try:
        res = requests.post(url, json={"query": update_query}, headers=headers_sys)
        print(f"   UPDATE assets -> Статус: {res.status_code}, Ответ: {res.text.strip()}")
    except Exception as e:
        print(f"   Ошибка соединения: {e}")

    # 2. ТЕСТ: Роль AI (Живой Интеллект)
    print("\n🧠 Тестирование роли AI (Ожидаем 403 на UPDATE и 200 на SELECT)...")
    headers_ai = {"X-Token": "uport_ai_secret"}
    try:
        res_update = requests.post(url, json={"query": update_query}, headers=headers_ai)
        print(f"   UPDATE assets -> Статус: {res_update.status_code} (Ожидалось 403), Ответ: {res_update.text.strip()}")
        
        res_select = requests.post(url, json={"query": select_query}, headers=headers_ai)
        print(f"   SELECT assets -> Статус: {res_select.status_code} (Ожидалось 200)")
    except Exception as e:
        print(f"   Ошибка соединения: {e}")

    # 3. ТЕСТ: Фейковый хакерский токен
    print("\n🕵️ Тестирование со взломанным токеном (Ожидаем 403 на всё)...")
    headers_fake = {"X-Token": "wrong_secret_pass"}
    try:
        res_fake = requests.post(url, json={"query": select_query}, headers=headers_fake)
        print(f"   SELECT assets -> Статус: {res_fake.status_code} (Ожидалось 403), Ответ: {res_fake.text.strip()}")
    except Exception as e:
        print(f"   Ошибка соединения: {e}")

if __name__ == "__main__":
    test_security()
