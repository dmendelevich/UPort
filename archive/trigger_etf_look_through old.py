#!/usr/bin/env python3
import sys
import asyncio
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
import yfinance as yf
import pandas as pd

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent))

from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient
import utils

# Настройка логирования для крона
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

async def run_etf_look_through_decomposition():
    print("\n" + "="*95)
    logging.info("🔬 [UPort LAB]: Старт Сита Декомпозиции Лаборатории ETF...")
    print("="*95)
    
    # 1. ФАБРИКА СВЯЗЕЙ БРОКЕРА: Собираем ровно ОДИН живой клиент на старте процесса
    logging.info("Factory: Инициализирую системный транспорт Freedom Broker на ключах Boss...")
    fb_client = FreedomBrokerClient.create_for_user(user_id=1, db_instance=db_sys)
    
    if not fb_client:
        logging.error("❌ Критическая ошибка: Не удалось собрать фабричный клиент брокера. Скрипт остановлен.")
        return
        
    # 2. ЗРЯЧАЯ СЕЛЕКЦИЯ: Выгребаем только те ETF, которые реально заведены в листингах системы
    sql_get_active_etfs = """
        SELECT DISTINCT t.id AS ticker_id, t.symbol, t.ticker_name_map ->> 'YAHOO' AS yahoo_symbol
        FROM public.tickers t
        INNER JOIN public.listings l ON l.ticker_id = t.id
        WHERE t.asset_type = 'ETF';
    """
    
    logging.info("📡 Запрашиваю из ядра список активных инвестиционных фондов ETF...")
    active_etfs = db_sys.execute_query(sql_get_active_etfs)
    
    if not active_etfs:
        logging.warning("info: В таблице листингов СУБД прямо сейчас не обнаружено фондов ETF для раскрытия.")
        return

    total_etfs = len(active_etfs)
    logging.info(f"📊 Обнаружено живых фондов в Universe: {total_etfs} шт. Запуск линейного конвейера...")
    print("-" * 95)

    processed_count = 0
    
    # 3. ЛИНЕЙНЫЙ ЦИКЛ РАСКРЫТИЯ СТРУКТУРЫ ФОНДОВ
    for etf in active_etfs:
        processed_count += 1
        t_id = int(etf["ticker_id"])
        symbol = etf["symbol"]
        yahoo_symbol = etf["yahoo_symbol"]
        
        if not yahoo_symbol:
            logging.warning(f"⏩ [ФОНД {processed_count}/{total_etfs}] '{symbol}' (ID={t_id}) не имеет Yahoo-тикера в карте имен. Пропуск.")
            continue
            
        logging.info(f"🧱 [ФОНД {processed_count}/{total_etfs}]: Разбор {symbol} (Yahoo-ключ: {yahoo_symbol}, ID={t_id})...")
        
        try:
            # Делаем строго одиночный, изолированный вызов по карте имен без текстовых .replace дефисов
            ticker_obj = yf.Ticker(yahoo_symbol)
            
            holdings_df = None
            try:
                if hasattr(ticker_obj, 'funds_data') and ticker_obj.funds_data and hasattr(ticker_obj.funds_data, 'top_holdings'):
                    holdings_df = ticker_obj.funds_data.top_holdings
            except Exception as funds_err:
                logging.warning(f"   ⚠️ Сбой обращения к funds_data для {yahoo_symbol}: {funds_err}")

            if holdings_df is not None and hasattr(holdings_df, 'empty') and not holdings_df.empty:
                holdings_df = holdings_df.reset_index()
                holdings_data = holdings_df.to_dict(orient='records')
            else:
                holdings_data = []

            if not holdings_data:
                logging.warning(f"   ⚠️ Не удалось извлечь внутренние холдинги для фонда {symbol}.")
                # Делаем обязательную паузу безопасности даже при пустом ответе
                await asyncio.sleep(1.5)
                continue

            # Построчный разбор компонентов состава фонда
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
                
                # 🔥 ШАГ 3.1: АВТОНОМНЫЙ ЗАЩИТНЫЙ КАПКАН КОМПОНЕНТА
                try:
                    # Легализация с пробросом готовой фабрики через Главные Ворота
                    comp_id, comp_listing_id = await asyncio.to_thread(
                        db_sys.ensure_ticker_v3,
                        ticker_name_raw=comp_global_symbol,
                        caller_role="ETF_LT",
                        caller_id=int(t_id),
                        broker_id=1,
                        fb_client=fb_client
                    )

                    if not comp_id:
                        continue

                    # Прицельно проверяем кэш СУБД по составному первичному ключу фонда и компонента
                    sql_check_holding = f"""
                        SELECT weight_percentage FROM public.etf_holdings 
                        WHERE etf_ticker_id = {t_id} 
                          AND component_ticker_id = {int(comp_id)} 
                        LIMIT 1;
                    """
                    existing_holding = db_sys.execute_query(sql_check_holding)

                    if existing_holding and len(existing_holding) > 0:
                        # ВЕТКА UPDATE: Извлекаем ПЕРВЫЙ элемент [0] из списка ответов СУБД!
                        row_h = existing_holding[0] if isinstance(existing_holding, list) else existing_holding
                        old_weight = float(row_h.get("weight_percentage") or 0)
                        
                        if abs(old_weight - weight) > 0.001:
                            sql_update_holding = f"""
                                UPDATE public.etf_holdings 
                                SET weight_percentage = {weight:.2f},
                                    last_updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                                WHERE etf_ticker_id = {t_id} 
                                  AND component_ticker_id = {int(comp_id)};
                            """
                            db_sys.execute_query(sql_update_holding)
                    else:
                        # ВЕТКА INSERT: Впервые заносим новый компонент в структуру фонда
                        sql_insert_holding = f"""
                            INSERT INTO public.etf_holdings (
                                etf_ticker_id, component_ticker_id, weight_percentage, last_updated_at
                            ) VALUES (
                                {t_id}, {int(comp_id)}, {weight:.2f}, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                            );
                        """
                        db_sys.execute_query(sql_insert_holding)
                        
                except Exception as comp_err:
                    # Если один экзотический компонент (вроде фьючерса FGTXX) отвалился — 
                    # фонд продолжает жить, скрипт переходит к следующей акции!
                    logging.warning(f"   ⚠️ [КОМПОНЕНТ ПРОПУЩЕН]: Сбой легализации '{comp_global_symbol}' внутри фонда: {comp_err}")
                    continue
                    
            logging.info(f"   ✅ Структура фонда {symbol} успешно раскрыта. Компоненты связаны.")

        except Exception as fund_err:
            logging.error(f"❌ Ошибка декомпозиции на фонде {symbol}: {fund_err}")
            
        # 🔥 ЗАГРАДИТЕЛЬНЫЙ РУБЕЖ ЗАЩИТЫ YAHOO FINANCE: Пауза 1.5 секунды между фондами
        await asyncio.sleep(1.5)

    print("\n" + "="*95)
    print("🏁 [ДЕКОМПОЗИЦИЯ ЗАВЕРШЕНА]: Все активные фонды Universe успешно выпотрошены!")
    print("="*95 + "\n")

if __name__ == "__main__":
    asyncio.run(run_etf_look_through_decomposition())
