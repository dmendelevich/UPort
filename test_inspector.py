#!/usr/bin/env python3
import sys
import logging
from pathlib import Path

# Фиксируем корень проекта для правильных импортов
sys.path.append(str(Path(__file__).parent.resolve()))

# Импортируем ядро СУБД и наш обновленный аналитический класс
from database import db_sys
from analytics.portfolio_inspector import PortfolioInspector

def run_analytics_test():
    """
    🔬 Модернизированный тест калькуляторов класса PortfolioInspector.
    Проверяет новые СУБД-доли, рыночные цены листингов и аудит лимитов.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    print("\n==================================================")
    print("🚀 [UPort Тест]: Запуск обновленной инспекции портфеля (ID=2)...")
    print("==================================================\n")
    
    try:
        # Инициализируем инспектора для портфеля
        inspector = PortfolioInspector(db_instance=db_sys, portfolio_id=2)
        
        # 1. ТЕСТ ГЛОБАЛЬНОГО ДЕНЕЖНОГО КОТЛА (По ценам listings.last_price)
        total_cap = inspector.calculate_total_capital()
        print(f"💵 [РЕЗУЛЬТАТ] Полный рыночный капитал портфеля: ${total_cap:,.2f}")
        
        # 2. ТЕСТ ВИРТУАЛЬНОГО РЕНТГЕНА КЭША ПО СТРАТЕГИЯМ
        balances = inspector.get_virtual_cash_balances()
        
        print("\n📐 Распределение ресурсов и виртуального кэша:")
        for s_id, data in balances["strategies"].items():
            print(f" ───────")
            print(f" 🧱 Стратегия: '{data['strategy_name']}' (Доля: {data['target_share_pct']}%)")
            print(f"   • Идеальный бюджет стратегии: ${data['ideal_budget_usd']:,.2f}")
            print(f"   • Рыночная стоимость акций:  ${data['current_holdings_usd']:,.2f}")
            print(f"   • Свободный виртуальный кэш: ${data['virtual_free_cash_usd']:,.2f}")
            
        # 3. ТЕСТ СЛУЖБЫ БЕЗОПАСНОСТИ (Аудит лимитов, секторов и налогов)
        print("\n🔒 Проверка лимитов и налогового предохранителя:")
        audit = inspector.audit_limits_and_rules()
        
        if not audit["has_violations"]:
            print(" ✅ [УСПЕХ] Нарушений лимитов и налогового щита не обнаружено.")
        else:
            print(" ⚠️ [ВНИМАНИЕ] Обнаружены нарушения правил управления рисками:")
            for s_id, strat_res in audit["strategies"].items():
                if strat_res["violation_found"]:
                    print(f"\n   🔴 В стратегии '{strat_res['strategy_name']}':")
                    
                    # Выводим нарушения по акциям (>5%)
                    for asset in strat_res["violated_assets"]:
                        print(f"     • Превышен лимит акции {asset['symbol']}: {asset['current_share_pct']}% (Лимит: {asset['limit_pct']}%)")
                        
                    # Выводим нарушения по секторам (>25%)
                    for sector in strat_res["violated_sectors"]:
                        print(f"     • Превышен лимит сектора '{sector['sector']}': {sector['current_share_pct']}% (Лимит: {sector['limit_pct']}%)")
                        
                    # Выводим нарушения налогового щита (дивиденды)
                    for breach in strat_res["tax_shield_breaches"]:
                        print(f"     • Налоговый щит пробит! Бумага {breach['symbol']} платит {breach['dividend_yield_pct']}% (Лимит: {breach['limit_pct']}%)")
            
    except Exception as e:
        print(f"\n❌ [КРИТИЧЕСКАЯ ОШИБКА ТЕСТА]: {e}")
        
    print("\n==================================================")
    print("🏁 [UPort Тест]: Инспекция завершена.")
    print("==================================================\n")

if __name__ == "__main__":
    run_analytics_test()
