#!/usr/bin/env python3
import os
import sys
import time
import json
import pandas as pd
import numpy as np
import logging
from pathlib import Path
from dotenv import load_dotenv
import yfinance as yf

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули базы и утилит
sys.path.append(str(Path(__file__).parent.parent.resolve()))
from database import db_sys
import utils
import config
from brokers_connectors.fb_client import FreedomBrokerClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def sync_freedom_signals(single_symbol=None):
    print("\n" + "="*120)
    print("🌅 [UPort MORNING CONVEYER v9.0]: Глобальный каскадный сборщик (Freedom Broker + Резерв Yahoo Finance)...")
    print("="*120)

    # Загружаем ключи авторизации брокера из .env файла ядра системы UPort
    env_path = Path(__file__).parent.parent.resolve() / ".env"
    load_dotenv(dotenv_path=env_path)
    fb_pub, fb_priv = os.getenv("FB_DLM_API_KEY"), os.getenv("FB_DLM_API_SECRET")
    
    if not fb_pub or not fb_priv:
        logging.error("❌ Ключи Freedom Broker не найдены в .env файле! Конвейер остановлен.")
        return

    # 🔥 ШАГ 1: КЭШИРОВАНИЕ КУРСОВ ВАЛЮТ (Скорректировано под from_currency, to_currency, rate)
    logging.info("💸 Загружаю актуальные курсы валют из public.currency_rates для пересчета оборотов в USD...")
    sql_rates = "SELECT from_currency, rate FROM public.currency_rates WHERE to_currency = 'USD';"
    db_rates = db_sys.execute_query(sql_rates)
    rates_cache = {str(r["from_currency"]).upper().strip(): float(r["rate"]) for r in db_rates if r.get("rate")} if db_rates else {}
    rates_cache["USD"] = 1.0  # Базовый доллар всегда равен единице

    # 🔥 ШАГ 2: SQL-ЗАПРОС НА ВЕСЬ UNIVERSE ТИКЕРОВ (ПОДДЕРЖИВАЕТ ТОЧЕЧНУЮ ОТЛАДКУ)
    if single_symbol:
        logging.info(f"🎯 [РЕЖИМ ОТЛАДКИ]: Сбор котировок изолирован строго под символ: {single_symbol}")
        # 🔥 ИСПРАВЛЕНО НАМЕРТВО: Заменили старый fb_ticker на ticker_name_map для режима отладки!
        sql_get_universe = f"SELECT id, symbol, ticker_name_map, current_price FROM public.tickers WHERE symbol = '{str(single_symbol).strip().upper()}';"
    else:
        logging.info("📡 Запрашиваю полный Universe тикеров (S&P 500, S&P 400 Mid-Cap, FTSE 100) из СУБД...")
        sql_get_universe = """
            SELECT id, symbol, ticker_name_map, current_price FROM public.tickers 
            WHERE provenance::text LIKE '%"MS_%' 
               OR provenance::text LIKE '%WL_ID=%' 
               OR provenance::text LIKE '%USER_ID=%';
        """
    
    active_rows = db_sys.execute_query(sql_get_universe)
    if not active_rows:
        logging.warning("⚠️ Инструментов для обработки в Universe СУБД не обнаружено.")
        return

    logging.info(f"📊 В обойму конвейера зашло: {len(active_rows)} акций. Инициализация сетевых шлюзов...")
    fb_client = FreedomBrokerClient(public_key=fb_pub, private_key=fb_priv)
    total_updated, processed_in_superchunk = 0, 0

    with utils.timer("Глобальный технический скоринг UPort"):
        # 🔥 ШАГ 3: РАЗВЕРТЫВАНИЕ КОНВЕЙЕРА ПАКЕТНЫХ ЭШЕЛОНОВ ИЗ CONFIG.PY ЧЕРЕЗ UTILS
        # 🔥 СТАРТ ПАКЕТНОГО КОНВЕЙЕРА (Проходим по эшелонам)
        for echelon_idx, db_batch in utils.create_echelons(active_rows, chunk_size=config.CHUNK_SIZE_SIGNALS):
            broker_tickers_list = []
            fb_to_db_map = {}
            yahoo_fallback_list = []  # Сюда складываем UNSUPPORTED инструменты для Потока Б
            
            for row in db_batch:
                db_id, symbol = row["id"], row["symbol"]
                
                # 🔥 ИСПРАВЛЕНО: Извлекаем карту имен и достаем из неё ключ "FB"
                name_map = row.get("ticker_name_map") or {}
                fb_code = name_map.get("FB", "UNSUPPORTED")
                
                # Если в карте написано конкретное имя — бумага летит в пул брокера (Поток А)
                if fb_code and fb_code != "UNSUPPORTED" and fb_code != "NULL":
                    fb_code_upper = str(fb_code).strip().upper()
                    broker_tickers_list.append(fb_code_upper)
                    fb_to_db_map[fb_code_upper] = {"db_id": db_id, "symbol": symbol}
                else:
                    # Если ФБ не поддерживает бумагу — она автоматически улетает в Резерв Yahoo (Поток Б)
                    yahoo_fallback_list.append({"db_id": db_id, "symbol": symbol})

            # =======================================================================================================
            # 🔥 ПОТОК А: МОДЕРНИЗИРОВАННЫЙ КОНВЕЙЕР FREEDOM BROKER С КОЛЬЦЕВЫМ БУФЕРОМ PRICE_HISTORY
            # =======================================================================================================
            if broker_tickers_list:
                tickers_string = ",".join(broker_tickers_list)
                params = {
                    "take": len(broker_tickers_list), "skip": 0,
                    "filter": {"filters": [{"field": "ticker", "operator": "in", "value": tickers_string}]}
                }
                
                try:
                    raw_response = fb_client.execute(command="getAllSecurities", params=params)
                    securities = raw_response.get("securities", []) if raw_response else []
                    if not securities and raw_response and "result" in raw_response:
                        result_node = raw_response.get("result", {})
                        if isinstance(result_node, dict): securities = result_node.get("securities", [])

                    successfully_updated_fb_codes = set()

                    for sec in securities:
                        fb_code = str(sec.get("ticker", "")).strip().upper()
                        node_map = fb_to_db_map.get(fb_code)
                        if not node_map: continue
                        t_id, symbol = node_map["db_id"], node_map["symbol"]

                        quotes_raw = sec.get("quotes", {})
                        quotes = {}
                        if isinstance(quotes_raw, dict): quotes = quotes_raw
                        elif isinstance(quotes_raw, str):
                            try: quotes = json.loads(quotes_raw)
                            except: quotes = {}

                        raw_ltp = quotes.get("ltp")
                        raw_pp = quotes.get("pp")
                        current_price = raw_ltp if raw_ltp is not None else raw_pp
                        
                        raw_rev = quotes.get("rev")
                        raw_vlt = quotes.get("vlt")
                        raw_curr = str(quotes.get("x_curr", "USD")).strip().upper()

                        # 🚨 ПЕРЕХВАТ 1: Если брокер вернул пустую цену — отправляем в резерв Yahoo
                        if current_price is None:
                            logging.info(f"   ⚠️ [ФБ ОГРАНИЧЕНИЕ]: Брокер вернул пустую цену для {symbol} ({fb_code}). Перенаправляю в резерв Yahoo...")
                            yahoo_fallback_list.append({"db_id": t_id, "symbol": symbol})
                            continue

                        successfully_updated_fb_codes.add(fb_code)

                        # Вытаскиваем мультипликаторы и валютные курсы
                        sql_curr_meta = f"SELECT multiplier FROM public.currencies WHERE id = '{raw_curr}' LIMIT 1;"
                        res_curr_meta = db_sys.execute_query(sql_curr_meta)
                        curr_multiplier = float(res_curr_meta[0]["multiplier"]) if res_curr_meta and len(res_curr_meta) > 0 and res_curr_meta[0].get("multiplier") else 1.0

                        db_sys.ensure_currency(raw_curr)
                        currency_rate = float(rates_cache.get(raw_curr, 1.0))

                        # Расчет оборота торгов в USD
                        if raw_vlt is not None:
                            vlt_sql = f"{float(raw_vlt) * curr_multiplier * currency_rate:.2f}"
                        else:
                            vlt_sql = "NULL"

                        current_price_sql = f"{current_price:.4f}"
                        raw_mkt_name = sec.get("mkt_name")
                        raw_codesub_nm = sec.get("codesub_nm")
                        fb_market_sql = f"'{str(raw_mkt_name).strip().replace("'", "''")}'" if raw_mkt_name else "NULL"
                        fb_exchange_sql = f"'{str(raw_codesub_nm).strip().replace("'", "''")}'" if raw_codesub_nm else "NULL"

                        # 🧮 ИНИЦИАЛИЗАЦИЯ ИНДИКАТОРОВ ПО УМОЛЧАНИЮ КАК NULL
                        ema20_sql, sma50_sql, sma100_sql, sma200_sql = "NULL", "NULL", "NULL", "NULL"
                        pct100_sql, pct200_sql = "NULL", "NULL"
                        rsi_sql, macd_sql = "NULL", "NULL"
                        recommendation = "NEUTRAL"

                        # 📦 ИЗВЛЕКАЕМ КЭШ КОЛЬЦА ИЗ СУБД ИЗ ОБНОВЛЕННОЙ КОЛОНКИ
                        sql_ring = f"SELECT price_history FROM public.tickers WHERE id = {t_id} LIMIT 1;"
                        res_ring = db_sys.execute_query(sql_ring)
                        
                        raw_history_list = None
                        if res_ring and isinstance(res_ring, list) and len(res_ring) > 0:
                            # Драйвер psycopg2 автоматически распарсит JSONB в список Python
                            raw_history_list = res_ring[0].get("price_history")

                        # 🚀 БЛОК АВТОМАТИЧЕСКОЙ ЗАЛИВКИ И САМОЗАЛЕЧИВАНИЯ СТРУКТУРЫ КОЛЬЦА
                        is_split_detected = False
                        
                        # Если кольцо уже существует, проверяем его на критический сплит/гэп по барьеру из config.py
                        if raw_history_list and isinstance(raw_history_list, list) and len(raw_history_list) > 0:
                            last_db_price = float(raw_history_list[-1])
                            # Сравниваем утреннюю цену ФБ с последней ценой из Кольца
                            price_change_pct = abs(((current_price / last_db_price) - 1) * 100)
                            
                            # Берём барьер из config.py (например, config.CRITICAL_SPLIT_PCT = 40.0)
                            critical_barrier = getattr(config, "CRITICAL_SPLIT_PCT", 40.0)
                            if price_change_pct >= critical_barrier:
                                logging.warning(f"   ⚠️ [САМОЗАЛЕЧИВАНИЕ ТРИГГЕР]: Обнаружен бросок цены на {price_change_pct:.2f}% для '{symbol}'. Запускаю регенерацию массива...")
                                is_split_detected = True

                        # Если Кольцо пустое (NULL) ИЛИ обнаружен критический сплит — стягиваем эталонный массив из Yahoo
                        if not raw_history_list or not isinstance(raw_history_list, list) or len(raw_history_list) < 200 or is_split_detected:
                            try:
                                logging.info(f"   🌐 [YAHOO REGENERATION]: Пакетный сбор годовой истории для '{symbol}'...")
                                sql_yf = f"SELECT yahoo_symbol FROM public.tickers WHERE id = {t_id} LIMIT 1;"
                                res_yf = db_sys.execute_query(sql_yf)
                                
                                # Забираем имя для Yahoo из базы или используем базовый символ
                                if res_yf and isinstance(res_yf, list) and len(res_yf) > 0:
                                    yf_name = res_yf[0].get("yahoo_symbol") or symbol
                                else:
                                    yf_name = symbol

                                ticker_init = yf.Ticker(yf_name)
                                hist_init = ticker_init.history(period="1y")
                                
                                if hist_init is not None and not hist_init.empty and len(hist_init) >= 200:
                                    # 🔥 НАШ СКВОЗНОЙ ПАТТЕРН: Умножаем ВСЮ историю из Yahoo на мультипликатор из базы.
                                    # Лондонские пенсы превратятся в фунты, а доллары останутся долларами!
                                    raw_prices = [float(p) * curr_multiplier for p in hist_init['Close'].values]
                                    # Обрезаем массив строго под эталонную длину 250 торговых дней
                                    raw_history_list = raw_prices[-250:]
                                    
                                    # Сразу фиксируем обновленный массив в СУБД в текстовом JSON-формате
                                    json_ring_sql = f"'{json.dumps(raw_history_list)}'::jsonb"
                                    db_sys.execute_query(f"UPDATE public.tickers SET price_history = {json_ring_sql} WHERE id = {t_id};")
                                    logging.info(f"   ✅ [КОЛЬЦО ИСЦЕЛЕНО]: В базу успешно записано {len(raw_history_list)} дней для '{symbol}'.")
                                else:
                                    logging.warning(f"   ⚠️ Yahoo Finance не вернул достаточную историю для '{symbol}'. Использование Кольца временно заблокировано.")
                            except Exception as init_err:
                                logging.error(f"   🚨 Критический сбой автонаполнения Кольца для '{symbol}': {init_err}")

                        # 🔄 БЛОК ЕЖЕДНЕВНОГО ПРОВОРОТА КОЛЬЦА И ГЛУБОКОГО ВЕКТОРНОГО ОБСЧЕТА ИНДИКАТОРОВ
                        if raw_history_list and isinstance(raw_history_list, list) and len(raw_history_list) >= 200:
                            try:
                                # Превращаем наш массив цен в классическую серию Pandas для мгновенных расчетов
                                # Цены в Кольце уже лежат с учетом мультипликатора (в фунтах/долларах)
                                history_series = pd.Series(raw_history_list)

                                # Проверяем факт новой сессии (Защита от выходных и праздников)
                                # Сдвигаем Кольцо только если сегодняшняя цена ФБ реально отличается от последнего дня
                                # или если объемы торгов показывают активность на бирже
                                is_new_session = True
                                if len(raw_history_list) > 0 and current_price == raw_history_list[-1]:
                                    # Если цена копейка в копейку равна прошлой, проверяем время сделки или объемы
                                    # Для простоты: в выходные сессия не сдвигается, массив остается прежним
                                    is_new_session = False

                                if is_new_session:
                                    # 🔥 ИСПРАВЛЕНО (ПО НАШЕМУ РЕГЛАМЕНТУ): Сначала проворачиваем Кольцо в памяти!
                                    # Выкидываем самый старый (первый) день из истории
                                    history_series = history_series.iloc[1:]
                                    # Дописываем свежую сегодняшнюю цену ФБ в самый конец массива
                                    history_series = pd.concat([history_series, pd.Series([current_price])], ignore_index=True)
                                    
                                    # Сохраняем прокрученный массив обратно в исходный список для записи в СУБД
                                    raw_history_list = [float(v) for v in history_series.values]

                                # 🧮 ЗАПУСК МЕГА-МАТЕМАТИКИ ИНДИКАТОРОВ НА ОБНОВЛЕННОМ МАССИВЕ PANDAS
                                current_calc_price = float(history_series.iloc[-1])
                                
                                # Расчет скользящих средних
                                ema_20 = float(history_series.ewm(span=20, adjust=False).mean().iloc[-1])
                                sma_50 = float(history_series.tail(50).mean())
                                sma_100 = float(history_series.tail(100).mean())
                                sma_200 = float(history_series.tail(200).mean())
                                
                                # Расчет процентов отклонения текущей цены от линий средних
                                price_to_sma100_pct = ((current_calc_price / sma_100) - 1) * 100
                                price_to_sma200_pct = ((current_calc_price / sma_200) - 1) * 100
                                
                                # Расчет RSI-14 по методу Уайлдера (Симметрично Потоку Б)
                                delta = history_series.diff()
                                gain = (delta.where(delta > 0, 0)).copy()
                                loss = (-delta.where(delta < 0, 0)).copy()
                                
                                avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
                                avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
                                
                                import numpy as np
                                rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
                                rsi_series = 100 - (100 / (1 + rs))
                                rsi_14 = float(rsi_series.iloc[-1])
                                
                                # Расчет Линии MACD (EMA_12 - EMA_26)
                                ema_12 = history_series.ewm(span=12, adjust=False).mean()
                                ema_26 = history_series.ewm(span=26, adjust=False).mean()
                                macd_line = ema_12 - ema_26
                                macd_val = float(macd_line.iloc[-1])

                                # Вычисление умной технической рекомендации
                                if current_calc_price > sma_200 and current_calc_price > sma_100:
                                    recommendation = "STRONG_BUY"
                                elif current_calc_price < sma_200 and current_calc_price < sma_100:
                                    recommendation = "SELL"
                                else:
                                    recommendation = "NEUTRAL"

                                # Форматируем все результаты в чистые SQL-строки
                                ema20_sql = f"{ema_20:.4f}"
                                sma50_sql = f"{sma_50:.4f}"
                                sma100_sql = f"{sma_100:.4f}"
                                sma200_sql = f"{sma_200:.4f}"
                                pct100_sql = f"{price_to_sma100_pct:.2f}"
                                pct200_sql = f"{price_to_sma200_pct:.2f}"
                                rsi_sql = f"{rsi_14:.2f}"
                                macd_sql = f"{macd_val:.4f}"

                            except Exception as math_err:
                                logging.error(f"   🚨 Ошибка вычисления индикаторов внутри Кольца для '{symbol}': {math_err}")

                        # Подготавливаем обновленный упакованный JSON-массив для СУБД
                        json_ring_update_sql = f"'{json.dumps(raw_history_list)}'::jsonb" if raw_history_list else "NULL"

                        # 🔥 ФИНАЛЬНЫЙ АТОМАРНЫЙ UPDATE СТРОКИ ТИКЕРА В СУБД (ЛИНИЯ ФБ)
                        # Сохраняем цену, оборот, все 6 рассчитанных индикаторов и обновленный массив цен
                        sql_update_fb = f"""
                            UPDATE public.tickers SET 
                                current_price = {current_price_sql}, 
                                currency_id = '{raw_curr}', 
                                daily_turnover_usd = {vlt_sql},
                                signal_recommendation = '{recommendation}', 
                                signal_ema_20 = {ema20_sql},
                                signal_sma_50 = {sma50_sql},
                                signal_sma_100 = {sma100_sql}, 
                                signal_price_to_sma100_pct = {pct100_sql},
                                signal_sma_200 = {sma200_sql}, 
                                signal_price_to_sma200_pct = {pct200_sql},
                                signal_rsi = {rsi_sql},
                                signal_macd = {macd_sql},
                                price_history = {json_ring_update_sql}, -- Фиксируем провернутый массив цен
                                signals_last_synced_at = CURRENT_TIMESTAMP, 
                                fb_market = {fb_market_sql}, 
                                fb_exchange = {fb_exchange_sql}
                            WHERE id = {t_id};
                        """
                        db_sys.execute_query(sql_update_fb)
                        total_updated += 1
                        logging.info(f"   ✅ [КОЛЬЦО УСПЕХ]: Полный мега-скоринг и сдвиг истории зафиксирован для '{symbol}'.")

                    # 🚨 ПЕРЕХВАТ 2: Ловим тихих зомби ФБ и перенаправляем в резерв Yahoo
                    for fb_code, node_map in fb_to_db_map.items():
                        if fb_code not in successfully_updated_fb_codes:
                            t_id, symbol = node_map["db_id"], node_map["symbol"]
                            logging.info(f"   ⚠️ [ФБ ИГНОР]: Брокер скрыл из ответа {symbol} ({fb_code}). Перенаправляю в Yahoo...")
                            yahoo_fallback_list.append({"db_id": t_id, "symbol": symbol})

                except Exception as batch_err:
                    logging.error(f"  ❌ Сбой Freedom-пачки в эшелоне №{echelon_idx}: {batch_err}")

            # =======================================================================================================
            # 🔥 ПОТОК Б: МОДЕРНИЗИРОВАННЫЙ РЕЗЕРВНЫЙ КОНТУР YAHOO FINANCE (Для сирот и европейских бумаг)
            # =======================================================================================================
            if yahoo_fallback_list:
                logging.info(f"   📡 [FALLBACK YAHOO v9.2]: Эшелон №{echelon_idx} запускает глубокий теханализ через yfinance для {len(yahoo_fallback_list)} акций...")
                for fallback_node in yahoo_fallback_list:
                    f_id, f_symbol = fallback_node["db_id"], fallback_node["symbol"]
                    
                    try:
                        # Читаем из базы эталонный yahoo_symbol и вычисленный паспортисткой MIC
                        res_yf_sym = db_sys.execute_query(f"SELECT yahoo_symbol, exchange_mic FROM public.tickers WHERE id = {f_id};")
                        if not res_yf_sym or len(res_yf_sym) == 0: continue
                        
                        row_yf = res_yf_sym[0]
                        yf_symbol = row_yf.get("yahoo_symbol")
                        f_mic = row_yf.get("exchange_mic")

                        if not f_mic:
                            logging.warning(f"   ⚠️ [FALLBACK СБОЙ]: Нет MIC в базе для {f_symbol}. Пропуск.")
                            continue

                        # Инициализируем объект Yahoo Finance
                        ticker_obj = yf.Ticker(yf_symbol)
                        
                        # 🔥 ИСПРАВЛЕНО: Меняем период с '1mo' на '1y', чтобы получить ~252 торговых дня истории
                        hist = ticker_obj.history(period="1y")
                        
                        if hist is not None and not hist.empty:
                            total_days = len(hist)
                            
                            # Находим текущую цену (самая последняя ячейка в колонке Close)
                            raw_yf_price = float(hist['Close'].iloc[-1])
                            raw_volume = float(hist['Volume'].iloc[-1]) if not hist['Volume'].empty else 0.0
                            
                            # Подтягиваем метаданные валюты биржи для расчета оборотов
                            sql_ex_meta = f"SELECT currency_id FROM public.exchanges WHERE mic = '{f_mic}' LIMIT 1;"
                            res_ex_meta = db_sys.execute_query(sql_ex_meta)
                            y_curr = str(res_ex_meta[0]["currency_id"]).strip().upper() if res_ex_meta and len(res_ex_meta) > 0 else "USD"
                            
                            sql_y_multiplier = f"SELECT multiplier FROM public.currencies WHERE id = '{y_curr}' LIMIT 1;"
                            res_y_mult = db_sys.execute_query(sql_y_multiplier)
                            y_multiplier = float(res_y_mult[0]["multiplier"]) if res_y_mult and len(res_y_mult) > 0 and res_y_mult[0].get("multiplier") else 1.0

                            # Применяем мультипликатор валюты (0.01 для лондонских пенсов, 1.0 для долларов)
                            clean_price = raw_yf_price * y_multiplier
                            
                            # Пересчитываем дневной оборот торгов в доллары США через rates_cache
                            y_rate = float(rates_cache.get(y_curr, 1.0))
                            calc_y_turnover_usd = (raw_volume * clean_price) * y_rate

                            # 🔥 СТАРТ ПАКЕТА ИНДИКАТОРОВ (Векторная математика Pandas)
                            # Инициализируем SQL-строки по умолчанию как NULL на случай короткой истории
                            ema20_sql, sma50_sql, sma100_sql, sma200_sql = "NULL", "NULL", "NULL", "NULL"
                            pct100_sql, pct200_sql = "NULL", "NULL"
                            recommendation = "NEUTRAL"

                            # Проверяем, достаточно ли торговых дней для расчета SMA-200
                            if total_days >= 200:
                                # 🧮 Вычисляем скользящие средние (Строго по нашему минитестеру)
                                ema_20 = float(hist['Close'].ewm(span=20, adjust=False).mean().iloc[-1])
                                sma_50 = float(hist['Close'].tail(50).mean())
                                sma_100 = float(hist['Close'].tail(100).mean())
                                sma_200 = float(hist['Close'].tail(200).mean())
                                
                                # Расчет процентов отклонения текущей цены (в пенсах/долларах) от линий средних
                                price_to_sma100_pct = ((raw_yf_price / sma_100) - 1) * 100
                                price_to_sma200_pct = ((raw_yf_price / sma_200) - 1) * 100
                                
                                # Вычисляем Умную техническую рекомендацию (Симметрично логике ФБ)
                                if raw_yf_price > sma_200 and raw_yf_price > sma_100:
                                    recommendation = "STRONG_BUY"
                                elif raw_yf_price < sma_200 and raw_yf_price < sma_100:
                                    recommendation = "SELL"
                                else:
                                    recommendation = "NEUTRAL"

                                # Форматируем числовые переменные в валидные SQL-строки
                                ema20_sql = f"{ema_20 * y_multiplier:.4f}"
                                sma50_sql = f"{sma_50 * y_multiplier:.4f}"
                                sma100_sql = f"{sma_100 * y_multiplier:.4f}"
                                sma200_sql = f"{sma_200 * y_multiplier:.4f}"
                                pct100_sql = f"{price_to_sma100_pct:.2f}"
                                pct200_sql = f"{price_to_sma200_pct:.2f}"

                                # 🧮 3. РАСЧЕТ RSI-14 (МАТЕМАТИКА PANDAS ИЗ МИНИТЕСТЕРА С ВЫРАВНИВАНИЕМ УАЙЛДЕРА)
                                # Находим ежедневную разницу в ценах закрытия
                                delta = hist['Close'].diff()
                                # Сегрегируем чистый рост (gain) и чистое падение (loss)
                                gain = (delta.where(delta > 0, 0)).copy()
                                loss = (-delta.where(delta < 0, 0)).copy()
                                
                                # Экспоненциальное скользящее среднее для 14 дней
                                avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
                                avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
                                
                                # Предотвращаем деление на ноль при нулевой волатильности
                                import numpy as np
                                rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
                                rsi_series = 100 - (100 / (1 + rs))
                                rsi_14 = float(rsi_series.iloc[-1])
                                rsi_sql = f"{rsi_14:.2f}"

                                # 🧮 4. РАСЧЕТ ЛИНИИ MACD (Быстрая EMA_12 - Медленная EMA_26)
                                ema_12 = hist['Close'].ewm(span=12, adjust=False).mean()
                                ema_26 = hist['Close'].ewm(span=26, adjust=False).mean()
                                macd_line = ema_12 - ema_26
                                macd_val = float(macd_line.iloc[-1])
                                macd_sql = f"{macd_val * y_multiplier:.4f}"
                            else:
                                rsi_sql = "NULL"
                                macd_sql = "NULL"

                            # 🔥 ФИНАЛЬНЫЙ СУПЕР-SQL ЗАПРОС К СУБД (БЕЗ ЗАТИРАНИЯ КАЛЕНДАРЯ ОТЧЕТОВ)
                            # Записываем все 6 рассчитанных индикаторов на годовом массиве Yahoo
                            sql_update_yhoo = f"""
                                UPDATE public.tickers SET 
                                    current_price = {clean_price:.4f}, 
                                    currency_id = '{y_curr}', 
                                    daily_turnover_usd = {calc_y_turnover_usd:.2f},
                                    signal_recommendation = '{recommendation}', 
                                    signal_ema_20 = {ema20_sql},
                                    signal_sma_50 = {sma50_sql},
                                    signal_sma_100 = {sma100_sql}, 
                                    signal_price_to_sma100_pct = {pct100_sql},
                                    signal_sma_200 = {sma200_sql}, 
                                    signal_price_to_sma200_pct = {pct200_sql}, 
                                    signal_rsi = {rsi_sql},
                                    signal_macd = {macd_sql},
                                    signals_last_synced_at = CURRENT_TIMESTAMP, 
                                    fb_market = 'YAHOO_FALLBACK', 
                                    fb_exchange = 'YAHOO_FALLBACK'
                                WHERE id = {f_id};
                            """
                            db_sys.execute_query(sql_update_yhoo)
                            total_updated += 1
                            logging.info(f"   ✅ [FALLBACK УСПЕХ]: Полный мега-скоринг для {f_symbol} записан через Yahoo.")
                            
                            # Небольшая пауза из config.py для бережной защиты Rate Limit
                            time.sleep(config.PAUSE_SIGNALS_SEC)
                        else:
                            logging.warning(f"   ⚠️ Yahoo не вернул историю для '{yf_symbol}'")
                    except Exception as yhoo_err:
                        logging.error(f"   🚨 Сбой глубокого анализа Yahoo для {f_symbol}: {yhoo_err}")

            # =======================================================================================================
            # 🔥 КОНТРОЛЬ ПАУЗ И СУПЕРПАУЗ КОНВЕЙЕРА ИЗ CONFIG.PY
            # =======================================================================================================
            processed_in_superchunk += config.CHUNK_SIZE_SIGNALS
            if processed_in_superchunk >= config.SUPERCHUNK_SIZE_SIGNALS and total_updated < len(active_rows):
                logging.info(f"⏳ [СУПЕРЧАНК ЗАВЕРШЕН]: Спим {config.SUPERCHUNK_PAUSE_SEC} сек...")
                time.sleep(config.SUPERCHUNK_PAUSE_SEC)
                processed_in_superchunk = 0
            else:
                time.sleep(config.PAUSE_SIGNALS_SEC)
                logging.info(f"   ✅ [ЭШЕЛОН №{echelon_idx} ЗАВЕРШЕН]: Накопительный итог: {total_updated} шт.")

    print("\n" + "="*120)
    print(f"🏁 [UPort MORNING CONVEYER COMPLETE]: Наполнено: {total_updated} из {len(active_rows)}!")
    print("="*120 + "\n")

if __name__ == "__main__":
    sync_freedom_signals()
