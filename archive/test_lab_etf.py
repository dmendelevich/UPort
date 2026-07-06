#!/usr/bin/env python3
import sys
import asyncio
import logging
from pathlib import Path

# Подгружаем системные пути ядра UPort, чтобы тестер видел модули экосистемы
sys.path.append(str(Path(__file__).parent.resolve()))

# Импортируем из нашего обновленного воркера целевую функцию декомпозиции
from site_connectors.trigger_etf_look_through import run_etf_look_through_decomposition

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def run_lab_etf_comprehensive_test():
    print("\n" + "="*95)
    print("🧪 [ТЕСТЕР ЛАБОРАТОРИИ ETF]: Запуск комплексной верификации контура декомпозиции")
    print("="*95)

    # 🎯 ТЕСТ №1: Эмуляция точечного вызова из Телеграм-бота для фонда COPX (ID=13)
    test_target_id = 13
    print(f"\n📥 [ТЕСТ 1]: Поступление команды от бота на мгновенное раскрытие фонда ID={test_target_id} (COPX)")
    print("-" * 95)
    
    try:
        await run_etf_look_through_decomposition(target_ticker_id=test_target_id)
        print(f"✅ [ТЕСТ 1 УСПЕХ]: Изолированный вызов для ID={test_target_id} отработал штатно.")
    except Exception as err1:
        print(f"❌ [ТЕСТ 1 СБОЙ]: Точечный вызов упал с ошибкой: {err1}")

    print("\n" + "─"*95)
    print("⏳ Ожидаю 3 секунды для разгрузки сетевого контура Yahoo...")
    await asyncio.sleep(3.0)
    print("─"*95)

    # 🌙 ТЕСТ №2: Эмуляция планового массового воскресного прогона хрона (без параметров)
    print("\n📥 [ТЕСТ 2]: Поступление команды от Cron на профилактический обход всего Universe...")
    print("-" * 95)
    
    try:
        await run_etf_look_through_decomposition(target_ticker_id=None)
        print("✅ [ТЕСТ 2 УСПЕХ]: Массовый профилактический прогон отработал штатно.")
    except Exception as err2:
        print(f"❌ [ТЕСТ 2 СБОЙ]: Массовый прогон упал с ошибкой: {err2}")

    print("\n" + "="*95)
    print("🏁 [ТЕСТИРОВАНИЕ ЗАВЕРШЕНО]: Проверьте данные и секунды времени в public.etf_holdings")
    print("="*95 + "\n")

if __name__ == "__main__":
    asyncio.run(run_lab_etf_comprehensive_test())
