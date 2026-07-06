import sys
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Настраиваем системные пути, чтобы тестер видел модули экосистемы UPort
sys.path.append(str(Path(__file__).parent))

from database import db_sys

async def run_gateway_test():
    print("🚀 СТАРТ ТЕСТИРОВАНИЯ ФОНДА ETF В ГЛАВНЫХ ВОРОТАХ UPort v3.0")
    print("──────────────────────────────────────────────────────────")
    
    # Загружаем .env для авторизации фабрики
    env_path = Path('/root/UPort/.env')
    load_dotenv(dotenv_path=env_path)
    
    # 🔍 СЦЕНАРИЙ №1: Эмуляция ввода реального мирового фонда ETF из ТГ
    test_etf_ticker = "VUSA.L"
    print(f"📥 [ТЕСТ ETF]: Ввод исследовательского фонда из ТГ: '{test_etf_ticker}'")
    
    try:
        t_id, l_id = await asyncio.to_thread(
            db_sys.ensure_ticker_v3,
            ticker_name_raw=test_etf_ticker, 
            caller_role="TG_USR", 
            caller_id=3, 
            broker_id=1,
            fb_client=None
        )
        print(f"  • Результат ТГ-вызова для фонда: ticker_id = {t_id} | listing_id = {l_id}")
    except Exception as err:
        print(f"  • ❌ Критический сбой Сценария ETF: {err}")

    print("\n──────────────────────────────────────────────────────────")
    print("⏳ Ожидаю завершения фонового обсчета сборщиков Yahoo в Event Loop...")
    await asyncio.sleep(5)
    
    print("\n🏁 Тестирование завершено! Проверьте СУБД.")

if __name__ == "__main__":
    asyncio.run(run_gateway_test())
