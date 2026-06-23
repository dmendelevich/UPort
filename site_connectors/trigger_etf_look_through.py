#!/usr/bin/env python3
import sys
import asyncio
import logging
from pathlib import Path
import yfinance as yf
import pandas as pd

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

# Импортируем из ядра только инстанс базы и саму асинхронную очередь задач
from database import db_sys, ETF_LOOK_THROUGH_QUEUE
import utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ─── ВСТРОЕННЫЙ МОДЕРНИЗИРОВАННЫЙ ВОРКЕР С ЗАЩИТОЙ КАРАНТИНА ───
async def lab_etf_queue_worker_loop(db_instance):
    """Локальный асинхронный исполнитель декомпозиции с фильтром ватчлиста UPort v4.0."""
    timestamp_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    
    while True:
        task = await ETF_LOOK_THROUGH_QUEUE.get()
        t_id = task["id"]             # ID родительского фонда в public.tickers
        symbol = task["symbol"]       # Тикер фонда
        full_ticker = task["full_ticker"]
        currency_id = task["currency_id"]
        broker_id = task["broker_id"]
        
        current_worker_pause = 1.0
        
        try:
            # 🛡️ ПРОВЕРКА КАРАНТИНА: Потрошим только те фонды, которые имеют метку WL_ID=
            sql_check_wl = f"SELECT provenance FROM public.tickers WHERE id = {t_id};"
            db_res = db_instance.execute_query(sql_check_wl)
            
            provenance_map = {}
            if db_res and isinstance(db_res, list) and len(db_res) > 0:
                provenance_map = db_res[0].get("provenance", {}) if "provenance" in db_res[0] else {}
                
            has_wl_marker = any(str(k).startswith("WL_ID=") for k in provenance_map.keys())
            
            if not has_wl_marker:
                # Фонд на карантине - лениво скипаем за миллисекунду!
                ETF_LOOK_THROUGH_QUEUE.task_done()
                await asyncio.sleep(0.01)
                continue
                
            # Прошел карантин - летим в Yahoo Finance за Top-10 составом!
            yf_symbol = symbol.replace('.', '-') if symbol else symbol
            ticker_obj = yf.Ticker(yf_symbol)
            info = await asyncio.to_thread(lambda: ticker_obj.info)
            
            q_type = info.get('quoteType', 'Unknown') if info else 'Unknown'
            is_etf = (q_type == 'ETF' or (info and info.get('expenseRatio') is not None))
            
            if not is_etf:
                current_worker_pause = 1.0
            else:
                logging.info(f"🧱 [Лаборатория ETF]: Фонд {full_ticker} (ID={t_id}) прошел карантин! Раскрываю структуру...")
                current_worker_pause = 1.0
                
                holdings_df = None
                if hasattr(ticker_obj, 'funds_data') and ticker_obj.funds_data and hasattr(ticker_obj.funds_data, 'top_holdings'):
                    holdings_df = ticker_obj.funds_data.top_holdings
                elif isinstance(info.get('holdings'), list):
                    holdings_df = pd.DataFrame(info.get('holdings'))

                if holdings_df is not None and hasattr(holdings_df, 'empty') and not holdings_df.empty:
                    holdings_df = holdings_df.reset_index()
                    holdings_data = holdings_df.to_dict(orient='records')
                else:
                    holdings_data = []

                if holdings_data:
                    for comp in holdings_data:
                        comp_sym = comp.get('Symbol') or comp.get('symbol') or comp.get('index') or comp.get('Ticker')
                        if not comp_sym:
                            continue
                            
                        raw_weight = comp.get('Holding Percent') or comp.get('holdingPercent') or comp.get('weight') or comp.get('Value') or 0
                        weight = float(raw_weight)
                        if 0 < weight < 1.0: 
                            weight = weight * 100
                        
                        if weight == 0:
                            continue
                            
                        comp_global_symbol = str(comp_sym).strip().upper()
                        
                        # Легализация карточки компонента через ядро
                        comp_id, comp_listing_id = await asyncio.to_thread(
                            db_instance.ensure_ticker_v2,
                            broker_id=broker_id,
                            broker_symbol=comp_global_symbol,
                            fallback_currency=currency_id,
                            fb_client=None
                        )
                        
                        if not comp_id:
                            continue

                        # 🔥 ВЖИВЛЕНИЕ ЖИВОГО ID РОДИТЕЛЬСКОГО ФОНДА: С флагом false для сохранения истории дат!
                        sql_update_provenance = f"""
                            UPDATE public.tickers
                            SET provenance = jsonb_set(provenance, '{{ETF_LT_ID={t_id}}}', '"{timestamp_str}"'::jsonb, true)
                            WHERE id = {comp_id} 
                              AND NOT (provenance ? 'ETF_LT_ID={t_id}');
                        """
                        db_instance.execute_query(sql_update_provenance)

                        sql_save_component = f"""
                            INSERT INTO public.etf_holdings (etf_ticker_id, component_ticker_id, weight_percentage, last_updated_at)
                            VALUES ({t_id}, {comp_id}, {weight:.2f}, CURRENT_TIMESTAMP)
                            ON CONFLICT (etf_ticker_id, component_ticker_id) 
                            DO UPDATE SET weight_percentage = EXCLUDED.weight_percentage, last_updated_at = CURRENT_TIMESTAMP;
                        """
                        db_instance.execute_query(sql_save_component)
                        
                    logging.info(f"   ✅ [Лаборатория ETF]: Разбор фонда {full_ticker} завершен. Компоненты связаны.")
                else:
                    logging.warning(f"   ⚠️ [Лаборатория ETF]: Не удалось извлечь внутренности для {full_ticker}.")

        except Exception as err:
            logging.error(f"❌ Ошибка локального воркера на тикере {full_ticker}: {err}")
        finally:
            ETF_LOOK_THROUGH_QUEUE.task_done()
            await asyncio.sleep(current_worker_pause)


async def main_forced_trigger():
    print("\n" + "="*95)
    print("🔬 [UPort LAB]: Автономный пуск асинхронного Сита Декомпозиции ETF...")
    print("="*95)
    
    # Выгребаем только фонды Босса и Папы, которые лежат в watchlist (имеют маркер WL_ID=)
    sql_get_active_etfs = """
        SELECT id, symbol, 'USD' AS currency_id 
        FROM public.tickers 
        WHERE asset_metadata IS NOT NULL 
          AND provenance::text LIKE '%WL_ID=%';
    """
    
    logging.info("📡 Запрашиваю из ядра список одобренных семьей фондов...")
    active_etfs = db_sys.execute_query(sql_get_active_etfs)
    
    if not active_etfs:
        logging.warning("ℹ️  В вашем реальном watchlist прямо сейчас не обнаружено фондов для раскрытия.")
        return

    logging.info(f"📊 Обнаружено живых фондов в ватчлисте: {len(active_etfs)} шт. Наполняю асинхронную очередь...")
    
    for etf in active_etfs:
        task_data = {
            "id": int(etf["id"]),
            "symbol": etf["symbol"],
            "full_ticker": etf["symbol"],
            "currency_id": etf["currency_id"],
            "broker_id": 1
        }
        ETF_LOOK_THROUGH_QUEUE.put_nowait(task_data)
        print(f"   📥 Фонд {etf['symbol']:6} (ID={etf['id']:3}) ➡️ Успешно заправлен в очередь задач!")
        
    print("-" * 95)
    logging.info("🚀 Запускаю изолированный событийный цикл декомпозиции...")
    print("-" * 95 + "\n")
    
    with utils.timer("Принудительная Look-Through декомпозиция"):
        # Создаем задачу для нашего встроенного автономного воркера
        worker_task = asyncio.create_task(lab_etf_queue_worker_loop(db_sys))
        
        # Ждем полного разгребания очереди задач
        await ETF_LOOK_THROUGH_QUEUE.join()
        await asyncio.sleep(5.0)
        worker_task.cancel()
        
    print("\n" + "="*95)
    print("🏁 [ЛАБОРАТОРНЫЙ ТЕСТ ЗАВЕРШЕН]: Все боевые фонды успешно выпотрошены!")
    print("="*95)
    
    # Контрольный срез результатов из СУБД
    sql_check = """
        SELECT symbol, provenance, daily_turnover_usd 
        FROM public.tickers 
        WHERE provenance::text LIKE '%ETF_LT_ID=%'
        LIMIT 5;
    """
    check_rows = db_sys.execute_query(sql_check)
    
    if check_rows:
        print("\n📊 КОНТРОЛЬНЫЙ СРЕЗ ВНОВЬ РОДИВШИХСЯ КОМПОНЕНТОВ В БАЗЕ:")
        print("-"*95)
        for row in check_rows:
            print(f"  ✅ Компонент: {row['symbol']:8} | Карта PROVENANCE: {row['provenance']}")
        print("-"*95 + "\n")

if __name__ == "__main__":
    asyncio.run(main_forced_trigger())
