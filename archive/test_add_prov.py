import time
import sys
from database import db_sys  # Используем максимальные инженерные права
from utils import manage_provenance

def run_live_test():
    ticker_target_id = 3395
    print(f"🚀 СТАРТ ТЕСТИРОВАНИЯ СИСТЕМЫ АВТОГРАФОВ ДЛЯ ID={ticker_target_id}")
    print("──────────────────────────────────────────────────────────")
    
    # ЭТАП 1: Добавляем 3 разных ключа
    print("📥 Шаг 1: Запись трех автографов...")
    
    r1 = manage_provenance(db_sys, ticker_id=ticker_target_id, source_key="USER_ID=3", action="add")
    print(f" • USER_ID=3: {'✅ Записан' if r1 else '❌ Ошибка'}")
    
    r2 = manage_provenance(db_sys, ticker_id=ticker_target_id, source_key="WL_ID=7", action="add")
    print(f" • WL_ID=7:   {'✅ Записан' if r2 else '❌ Ошибка'}")
    
    r3 = manage_provenance(db_sys, ticker_id=ticker_target_id, source_key="MS_SP500", action="add")
    print(f" • MS_SP500:  {'✅ Записан' if r3 else '❌ Ошибка'}")
    
    print("\n⏳ Пауза 3 секунды для проверки защиты от перезаписи даты...")
    time.sleep(3)
    
    # ЭТАП 2: Проверяем защиту даты (пробуем перезаписать USER_ID=3)
    print("🛡️ Шаг 2: Проверка блокировки перезаписи для USER_ID=3...")
    r4 = manage_provenance(db_sys, ticker_id=ticker_target_id, source_key="USER_ID=3", action="add")
    print(f" • Повторный вызов USER_ID=3: {'✅ Выполнен' if r4 else '❌ Ошибка'}")
    print("👉 Пожалуйста, сделайте паузу! Проверьте в СУБД, что у USER_ID=3 осталось СТАРЕНЬКОЕ время.")
    print("Нажмите ENTER в терминале, когда проверите базу, чтобы запустить Шаг 3 (УДАЛЕНИЕ)...")
    input()
    
    # ЭТАП 3: Удаляем ключ пользователя, оставляя другие
    print("🧹 Шаг 3: Удаление автографа USER_ID=3...")
    r_del = manage_provenance(db_sys, ticker_id=ticker_target_id, source_key="USER_ID=3", action="remove")
    print(f" • Результат удаления: {'✅ Успешно вырезан' if r_del else '❌ Ошибка'}")
    
    print("\n🏁 Тест полностью завершен. Проверьте финальный состав поля provenance в СУБД!")
    print("Ожидаемый финал: Ключ USER_ID=3 исчез, а WL_ID=7 и MS_SP500 остались нетронутыми.")

if __name__ == "__main__":
    run_live_test()
