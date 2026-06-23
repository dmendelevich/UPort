#!/usr/bin/env python3
import sys
import yfinance as yf
from pprint import pprint

def run_yahoo_scouting():
    # Будем тестировать на живой бумаге Procter & Gamble (PG)
    test_symbol = "PG"
    print(f"📡 [YAHOO SCOUTING]: Инициализация сырого запроса для тикера {test_symbol}...")
    
    try:
        ticker_obj = yf.Ticker(test_symbol)
        
        # === ЭТАП 1: Разведка словаря info (Целевые цены Уолл-стрит) ===
        print("\n🔍 --- 1. ПРОВЕРКА СЛОВАРЯ info И ЦЕЛЕВЫХ ЦЕН ---")
        info = ticker_obj.info
        if info and isinstance(info, dict):
            # Собираем все ключи, где есть упоминание "target" или "price"
            target_keys = {k: v for k, v in info.items() if 'target' in k.lower() or 'recommend' in k.lower()}
            print("Найденные сырые ключи аналитиков в info:")
            pprint(target_keys)
        else:
            print("⚠️ Yahoo не вернул info-профиль!")

        # === ЭТАП 2: Разведка таблицы отчетов (Для CAGR выручки П136) ===
        print("\n🔍 --- 2. ПРОВЕРКА ТАБЛИЦЫ ФИНАНСОВЫХ ОТЧЕТОВ financials ---")
        financials = ticker_obj.financials
        if financials is not None and not financials.empty:
            print("Доступные строки (индексы) в отчете financials:")
            print(list(financials.index))
            print("\nДоступные столбцы (даты/годы отчетов):")
            print(list(financials.columns))
            
            # Проверяем наличие строки выручки
            revenue_names = [idx for idx in financials.index if 'revenue' in idx.lower()]
            print(f"\nНайденные варианты строк выручки: {revenue_names}")
            if revenue_names:
                print("\nСрез данных по строке выручки:")
                print(financials.loc[revenue_names[0]])
        else:
            print("⚠️ Таблица financials пуста или недоступна!")

        # === ЭТАП 3: Разведка истории цен (Для технического RSI П10) ===
        print("\n🔍 --- 3. ПРОВЕРКА ИСТОРИИ ЦЕН ДЛЯ РАСЧЕТА RSI ---")
        hist = ticker_obj.history(period="1mo")
        if hist is not None and not hist.empty:
            print(f"Размер полученной таблицы истории: {hist.shape} (строк, колонок)")
            print("Первые 3 торговые сессии:")
            print(hist['Close'].head(3))
            print("Последние 3 торговые сессии:")
            print(hist['Close'].tail(3))
        else:
            print("⚠️ История цен недоступна!")

    except Exception as scouting_err:
        print(f"❌ Критический сбой при разведке Yahoo: {scouting_err}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_yahoo_scouting()
