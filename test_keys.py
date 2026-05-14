import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

fb_key = os.getenv("FB_DLM_API_KEY")
fb_secret = os.getenv("FB_DLM_API_SECRET")

if fb_key and fb_secret:
    print(f"✅ Ключи успешно загружены из .env!")
    print(f"Ключ начинается на: {fb_key[:5]}...")
else:
    print("❌ Ошибка: Ключи не найдены. Проверь файл .env")
