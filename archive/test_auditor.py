import json
import logging
from database import Database
from analytics.portfolio_auditor import generate_portfolio_passport, audit_ticker_for_portfolio

logging.basicConfig(level=logging.WARNING)

def run_full_audit_test():
    print("🧪 --- ТОТАЛЬНЫЙ ТЕСТ ОБОИХ КОНТУРОВ АУДИТОРА UPORT --- 🧪\n")
    db = Database(role="BOT")
    
    TEST_PORTFOLIO_ID = 1  # ID твоего портфеля П10
    
    # === ТЕСТ ФУНКЦИИ 1: КЛАССИЧЕСКИЙ ПАСПОРТ ПОРТФЕЛЯ ===
    print(f"💼 [КОНТУР 1]: Генерация паспорта портфеля ID = {TEST_PORTFOLIO_ID}...")
    passport = generate_portfolio_passport(portfolio_id=TEST_PORTFOLIO_ID, db_instance=db)
    print(f"   • Имя портфеля в базе: {passport['meta']['name']}")
    print(f"   • Всего нарушений лимитов портфеля: {len(passport['violations'])}")
    for v in passport['violations']:
        print(f"     ➔ {v}")
    print("\n" + "="*70 + "\n")

    # === ТЕСТ ФУНКЦИИ 2: КРОСС-АУДИТ КОНКРЕТНЫХ ТИКЕРОВ ДЛЯ ЭТОГО ПОРТФЕЛЯ ===
    print(f"📈 [КОНТУР 2]: Кросс-аудит акций для стратегии портфеля {passport['meta']['name']} (ID = {TEST_PORTFOLIO_ID})...")
    
    # Тест 1: Проверяем Procter & Gamble (налоговый нарушитель)
    ticker_1 = "PG.US"
    print(f"\n🔍 Проверка актива {ticker_1}:")
    warnings_1 = audit_ticker_for_portfolio(full_ticker=ticker_1, portfolio_id=TEST_PORTFOLIO_ID, db_instance=db)
    if warnings_1:
        for w in warnings_1:
            print(f"   {w}")
    else:
        print("   ✅ Актив идеально соответствует всем лимитам этого портфеля.")

    # Тест 2: Проверяем ASML (нарушитель по концентрации веса)
    ticker_2 = "ASML.US"
    print(f"\n🔍 Проверка актива {ticker_2}:")
    warnings_2 = audit_ticker_for_portfolio(full_ticker=ticker_2, portfolio_id=TEST_PORTFOLIO_ID, db_instance=db)
    if warnings_2:
        for w in warnings_2:
            print(f"   {w}")
    else:
        print("   ✅ Актив идеально соответствует всем лимитам этого портфеля.")

    # Тест 3: Проверяем Сводный портфель (ID = 0) — аудит должен автоматически отключиться
    print(f"\n🛡️ Проверка безопасности: Попытка вызвать аудит {ticker_1} для СВОДНОГО портфеля семьи (ID = 0)...")
    warnings_zero = audit_ticker_for_portfolio(full_ticker=ticker_1, portfolio_id=0, db_instance=db)
    print(f"   • Результат (ожидается пустой список []): {warnings_zero}")

if __name__ == "__main__":
    run_full_audit_test()
