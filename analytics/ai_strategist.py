import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# 🔥 ШАГ 1: НАМЕРТВО РАСШИРЯЕМ ПУТИ ПОИСКА ЯДРА (САМЫЙ ВЕРХ ИСПОЛНЕНИЯ!)
sys.path.append(str(Path(__file__).parent.parent))

# Теперь Python гарантированно и без ошибок увидит папки site_connectors и database
import psycopg2
from psycopg2.extras import RealDictCursor
from decimal import Decimal

# 🔥 ШАГ 2: СМЕЛО ИМПОРТИРУЕМ НАШ ВЫДЕЛЕННЫЙ ШЛЮЗ VSEGPT (СТРОГО НИЖЕ SYS.PATH!)
from site_connectors.vsegpt_client import request_vsegpt_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class DecimalEncoder(json.JSONEncoder):
    def default(self, o): 
        if isinstance(o, Decimal): return float(o)
        return super(DecimalEncoder, self).default(o)

def run_ai_portfolio_audit(portfolio_id: int):
    """Боевой ИИ-Оркестратор UPort v2.6 (Полностью очищенный от urllib)."""
    logging.info(f"🧠 [ИИ СТРАТЕГ]: Старт планового боевого аудита для portfolio_id = {portfolio_id}...")
    
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    db_params = {
        "host": os.getenv("DB_HOST"), "port": os.getenv("DB_PORT"),
        "database": os.getenv("DB_NAME"), "user": os.getenv("DB_USER"), "password": os.getenv("DB_PASS")
    }

    try:
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ─── ШАГ 1: ВЫГРУЖАЕМ ПАСПОРТ СТРАТЕГИИ ───
        cur.execute("""
            SELECT strategy_name, engine_type, rules_config, manifest, response_format
            FROM public.strategies WHERE portfolio_id = %s AND is_active = true LIMIT 1;
        """, (portfolio_id,))
        strategy_row = cur.fetchone()
        if not strategy_row: return

        # ─── ШАГ 2: СОБИРАЕМ КОШЕЛЕК И КЭШ ───
        cur.execute("""
            SELECT l.broker_symbol, a.quantity, a.avg_price, l.last_price,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
            FROM public.assets a JOIN public.listings l ON a.listing_id = l.id
            WHERE a.portfolio_id = %s AND a.quantity > 0;
        """, (portfolio_id,))
        assets_list = cur.fetchall()

        # 🔥 ВСЕВИДЯЩИЙ КОНТУР КЭША UPORT: собираем торговые остатки + накопительные D-счета владельца
        cur.execute("""
            SELECT account_type, currency_id, cash_available, cash_reserved 
            FROM public.accounts 
            WHERE portfolio_id = %s
               OR (account_type = 'deposit' AND user_id = (SELECT owner_id FROM public.portfolios WHERE id = %s));
        """, (portfolio_id, portfolio_id))
        cash_list = cur.fetchall()
        portfolio_facts = {"assets": assets_list or [], "cash_balances": cash_list or [], "audit_timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

        # ─── ШАГ 3: КРОСС-ДИВЕРСИФИКАЦИЯ СЕМЬИ ───
        cur.execute("""
            SELECT DISTINCT l.broker_symbol FROM public.assets a JOIN public.listings l ON a.listing_id = l.id WHERE a.portfolio_id != %s AND a.quantity > 0;
        """, (portfolio_id,))
        family_holdings = [row['broker_symbol'] for row in cur.fetchall()]

        # ─── ШАГ 4: ВЫГРУЖАЕМ ВИТРИНУ УОЛЛ-СТРИТ ───
        cur.execute("""
            SELECT DISTINCT t.symbol, l.broker_symbol, l.last_price, t.company_name, t.sector,
                   t.pe_trailing, t.pe_forward, t.dividend_yield, t.rsi_14, t.revenue_cagr_3y,
                   t.target_low_price, t.target_mean_price, t.target_high_price
            FROM public.watchlist w JOIN public.listings l ON w.listing_id = l.id JOIN public.tickers t ON l.ticker_id = t.id
            WHERE w.portfolio_id = %s;
        """, (portfolio_id,))
        market_intelligence = cur.fetchall()

        # ─── ШАГ 5: СБОРКА СИСТЕМНОГО ПРОМПТА ───
        system_prompt = (
            "Вы — Главный ИИ-Управляющий и Алгоритмический Стратег семейной инвестиционной экосистемы UPort.\n"
            "Ваша цель — жесткое управление капиталом на основе Инвестиционной декларации владельца конкретного счета.\n\n"
            "ЖЕСТКОЕ СИСТЕМНОЕ ОГРАНИЧЕНИЕ (ПРАВИЛО КРОСС-ДИВЕРСИФИКАЦИИ СЕМЬИ):\n"
            "Вам категорически запрещено дублировать активы между портфелями семьи. Полностью игнорируйте и отсекайте тикеры из блока 'FAMILY_HOLDINGS_SNAPSHOT'.\n\n"
            f"Структура ответа должна строго соответствовать шаблону response_format из СУБД:\n{json.dumps(strategy_row['response_format'], ensure_ascii=False)}\n"
        )

        user_content = {
            "portfolio_id": portfolio_id, "engine_type": strategy_row['engine_type'], "strategy_name": strategy_row['strategy_name'],
            "STRATEGY_CONFIG": strategy_row['rules_config'], "HUMAN_MANIFEST": strategy_row['manifest'],
            "PORTFOLIO_FACTS": portfolio_facts, "FAMILY_HOLDINGS_SNAPSHOT": family_holdings, "MARKET_INTELLIGENCE": market_intelligence or []
        }

        # ─── ШАГ 6: ВЫЗОВ НАШЕГО ИЗОЛИРОВАННОГО ИИ-МОДУЛЯ VSEGPT ───
        user_prompt_json = json.dumps(user_content, ensure_ascii=False, cls=DecimalEncoder)
        ai_json = request_vsegpt_json(system_prompt, user_prompt_json)

        # 🔥 УМНЫЙ СБРОС СТРУКТУРЫ UPORT: если ИИ вернул данные внутри ключа 'structure'
        if "structure" in ai_json:
            logging.info("📦 [ИИ СТРАТЕГ]: Обнаружена вложенная структура ответа. Распаковываю узел 'structure'...")
            ai_json = ai_json["structure"]
        
        if not ai_json:
            logging.error("❌ [ИИ СТРАТЕГ]: От ИИ-модуля прилетел пустой ответ. Отмена транзакции.")
            return

        logging.info("🧠 [ИИ СТРАТЕГ]: УСПЕШНЫЙ СКВОЗНОЙ ОТВЕТ ПОЛУЧЕН! Начинаю транзакционную запись...")
        
        # Фиксируем обоснование ИИ в СУБД
        ai_rationale = ai_json.get("ai_analysis_rationale", "No rationale provided.")
        cur.execute("UPDATE public.strategies SET ai_rationale = %s, updated_at = CURRENT_TIMESTAMP WHERE portfolio_id = %s;", (ai_rationale, portfolio_id))
        
        # ─── ШАГ 7: ТРАНЗАКЦИОННАЯ ЗАПИСЬ АЛЕРТОВ (3NF) ───
        actions = ai_json.get("recommended_actions", [])
        for act in actions:
            action_type = act.get("action_type")
            ticker_symbol = act.get("ticker")
            note_text = act.get("note", "Робот UPort: Срабатывание триггера ИИ.")
            if not ticker_symbol: continue
                
            cur.execute("SELECT id FROM public.listings WHERE broker_symbol = %s LIMIT 1;", (ticker_symbol,))
            listing_row = cur.fetchone()
            if not listing_row: continue
            l_id = listing_row["id"]
            
            if action_type in ("create_alert", "modify_alert", "rebalance_signal"):
                cond_type = act.get("condition_type", ">")
                trig_price = act.get("trigger_price")
                trig_pct = act.get("trigger_pct")
                t_type = act.get("trigger_type", "crossing")
                
                sql_insert_alert = """
                    INSERT INTO public.alerts (
                        portfolio_id, listing_id, source_type, auth_login, ticker, init_price, trigger_price, 
                        condition_type, trigger_type, is_active, updated_at, trigger_pct, note, created_by_user_id
                    ) VALUES (
                        %s, %s, 'uport', 'ai_assistant@uport.internal', %s, (SELECT last_price FROM public.listings WHERE id = %s), 
                        %s, %s, %s, true, CURRENT_TIMESTAMP, %s, %s, (SELECT owner_id FROM public.portfolios WHERE id = %s)
                    ) ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET
                        trigger_price = EXCLUDED.trigger_price, condition_type = EXCLUDED.condition_type,
                        trigger_type = EXCLUDED.trigger_type, trigger_pct = EXCLUDED.trigger_pct,
                        note = EXCLUDED.note, is_active = true, updated_at = CURRENT_TIMESTAMP;
                """
                cur.execute(sql_insert_alert, (portfolio_id, l_id, ticker_symbol, l_id, trig_price, cond_type, t_type, trig_pct, note_text, portfolio_id))
                logging.info(f"   ✅ [АЛЕРТ ВЗВЕДЕН ИИ]: {ticker_symbol} {cond_type} {trig_price or trig_pct}% — Зафиксировано в СУБД.")
                
        conn.commit()
        logging.info(f"⚡ [ИИ СТРАТЕГ]: Плановый аудит портфеля {portfolio_id} успешно завершен.")

    except Exception as global_ai_err:
        if 'conn' in locals() and conn: conn.rollback()
        logging.error(f"❌ [ИИ СТРАТЕГ КРИТИЧЕСКИЙ СБОЙ]: {global_ai_err}")
    finally:
        if 'cur' in locals() and cur: cur.close()
        if 'conn' in locals() and conn: conn.close()

if __name__ == "__main__":
    run_ai_portfolio_audit(portfolio_id=1)
