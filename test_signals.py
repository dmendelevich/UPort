#!/usr/bin/env python3
import sys
from pathlib import Path
import logging

# Подключаем пути проекта UPort
sys.path.append(str(Path(__file__).parent.resolve()))
sys.path.append(str(Path(__file__).parent / "site_connectors"))

# Вместо старого сборщика ФБ импортируем новый глобальный Yahoo-конвейер
from sync_signals_yf import sync_global_yahoo_signals as sync_freedom_signals

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_isolated_signals_test():
    print("\n" + "="*100)
    print("🚀 [UPort SIGNALS TESTER]: Запуск поочередного изолированного теста сборщика сигналов...")
    print("="*100)

    # Список калиброванных образцов (Америка, Европа и разделенные коллизии)
    test_symbols = [
        "AAPL",    # Чистая Америка (Линия ФБ)
        "BP",      # Американская BP в долларах (Линия ФБ)
        "BP.L",    # Лондонская BP в фунтах (Резервный контур Yahoo)
        "VUSA.L"   # Европейский ETF в Лондоне (Резервный контур Yahoo)
    ]

    for sym in test_symbols:
        print(f"\n👉 [TEST]: Передаю в конвейер одиночный символ: '{sym}'")
        try:
            # Вызываем сборщик в одиночном режиме.
            # Скрипт сам создаст эшелон из 1 элемента и обновит котировки/индикаторы в СУБД.
            sync_freedom_signals(single_symbol=sym)
        except Exception as err:
            logging.error(f"❌ Критический сбой при обработке символа '{sym}': {err}")

    print("\n" + "="*100)
    print("🏁 Изолированный тест сигналов завершен! База данных обновлена.")
    print("="*100 + "\n")

if __name__ == "__main__":
    run_isolated_signals_test()
