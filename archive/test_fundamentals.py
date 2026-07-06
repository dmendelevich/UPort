#!/usr/bin/env python3
import sys
from pathlib import Path
import logging

sys.path.append(str(Path(__file__).parent.resolve()))
sys.path.append(str(Path(__file__).parent / "site_connectors"))

from database import db_sys
from sync_fundamentals_yhoo import sync_fundamentals

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_extended_fundamental_test():
    print("\n" + "="*100)
    print("🚀 [UPort FUNDAMENTALS TESTER]: Запуск каскадного стресс-теста фундаментального контура...")
    print("="*100)

    # Обойма из 6 калиброванных образцов (акции, близнецы, фонды США и Лондона, коллизия Беркшир)
    test_symbols = [
        "AAPL",    # Акция США
        "BRK.B",  # Коллизия точки (Беркшир)
        "BP",      # Акция США (близнец)
        "BP.L",    # Акция Лондон (близнец в фунтах)
        "SPY",     # Фонд/ETF США
        "VUSA.L"   # Фонд/ETF Лондон в фунтах
    ]

    for sym in test_symbols:
        print(f"\n👉 [TEST]: Экстренный залёт в конвейер символа: '{sym}'")
        try:
            # Вызываем сборщик в одиночном режиме экстренного подхвата
            sync_fundamentals(db_sys, single_symbol=sym)
        except Exception as err:
            logging.error(f"❌ Критический сбой при обработке фундаментала '{sym}': {err}")

    print("\n" + "="*100)
    print("🏁 Каскадный стресс-тест завершен! База данных обновлена.")
    print("="*100 + "\n")

if __name__ == "__main__":
    run_extended_fundamental_test()
