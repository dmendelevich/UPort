#!/usr/bin/env python3
import os
import sys
import time
import json
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

    # 🔥 ШАГ 2: SQL-ЗАПРОС НА ВЕСЬ UNIVERSE ТТИКЕРОВ (ПОДДЕРЖИВАЕТ ТОЧЕЧНУЮ ОТЛАДКУ)
    if single_symbol:
        logging.info(f"🎯 [РЕЖИМ ОТЛАДКИ]: Сбор котировок изолирован строго под символ: {single_symbol}")
        sql_get_universe = f"SELECT id, symbol, fb_ticker FROM public.tickers WHERE symbol = '{str(single_symbol).strip().upper()}';"
    else:
        logging.info("📡 Запрашиваю полный Universe тикеров (S&P 500, S&P 400 Mid-Cap, FTSE 100) из СУБД...")
        sql_get_universe = """
            SELECT id, symbol, fb_ticker FROM public.tickers 
            WHERE provenance::text LIKE '%"MS_%' OR provenance::text LIKE '%WL_ID=%' OR provenance::text LIKE '%USER_ID=%' OR provenance::text LIKE '%system_gateway%';
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
        for echelon_idx, db_batch in utils.create_echelons(active_rows, chunk_size=config.CHUNK_SIZE_SIGNALS):
            broker_tickers_list = []
            fb_to_db_map = {}
            yahoo_fallback_list = []  # Сюда складываем сирот (Канада, Лондон), у которых fb_ticker IS NULL
            
            for row in db_batch:
                db_id, symbol, cached_fb = row["id"], row["symbol"], row.get("fb_ticker")
                
                if cached_fb and str(cached_fb).strip().upper() != "NULL":
                    fb_code = str(cached_fb).strip().upper()
                    broker_tickers_list.append(fb_code)
                    fb_to_db_map[fb_code] = {"db_id": db_id, "symbol": symbol}
                else:
                    yahoo_fallback_list.append({"db_id": db_id, "symbol": symbol})

            # =======================================================================================================
            # 🔥 ПОТОК А: ПАКЕТНЫЙ ЗАПРОС КОТИРОВОК У FREEDOM BROKER (Сбор основных данных и перехват отказников)
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

                    # Запоминаем, какие тикеры брокер успешно обновил, чтобы найти тех, кого он молча проигнорировал
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
                        
                        p22 = quotes.get("p22")
                        p110 = quotes.get("p110")
                        p220 = quotes.get("p220")
                        chg110 = quotes.get("chg110")
                        raw_rev = quotes.get("rev")
                        raw_vlt = quotes.get("vlt")
                        raw_curr = str(quotes.get("x_curr", "USD")).strip().upper()

                        # 🚨 ПЕРЕХВАТ 1: Если брокер вернул пустую цену (как для ARTY или новичков) — отправляем тикер в резерв Yahoo
                        if current_price is None:
                            logging.info(f"   ⚠️ [ФБ ОГРАНИЧЕНИЕ]: Брокер вернул пустую цену для {symbol} ({fb_code}). Перенаправляю в резерв Yahoo...")
                            yahoo_fallback_list.append({"db_id": t_id, "symbol": symbol})
                            continue

                        successfully_updated_fb_codes.add(fb_code)

                        # Вытаскиваем мультипликатор из public.currencies
                        sql_curr_meta = f"SELECT multiplier FROM public.currencies WHERE id = '{raw_curr}' LIMIT 1;"
                        res_curr_meta = db_sys.execute_query(sql_curr_meta)
                        curr_multiplier = float(res_curr_meta[0]["multiplier"]) if res_curr_meta and len(res_curr_meta) > 0 and res_curr_meta[0].get("multiplier") else 1.0

                        db_sys.ensure_currency(raw_curr)
                        currency_rate = float(rates_cache.get(raw_curr, 1.0))

                        if raw_vlt is not None:
                            vlt_sql = f"{float(raw_vlt) * curr_multiplier * currency_rate:.2f}"
                        else:
                            vlt_sql = "NULL"

                        price_to_sma200_pct_sql = f"{((float(current_price) / float(p220)) - 1) * 100:.2f}" if p220 is not None and float(p220) > 0 else "NULL"
                        if chg110 is not None: price_to_sma100_pct_sql = f"{float(chg110):.2f}"
                        elif p110 is not None and float(p110) > 0: price_to_sma100_pct_sql = f"{((float(current_price) / float(p110)) - 1) * 100:.2f}"
                        else: price_to_sma100_pct_sql = "NULL"

                        if p220 is not None and p22 is not None:
                            recommendation = "STRONG_BUY" if (current_price > p220 and current_price > p22) else ("SELL" if (current_price < p220 and current_price < p22) else "NEUTRAL")
                        elif p220 is not None: recommendation = "STRONG_BUY" if current_price > p220 else "SELL"
                        else: recommendation = "NEUTRAL"

                        current_price_sql = f"{current_price:.4f}"
                        p110_sql = f"{p110:.4f}" if p110 is not None else "NULL"
                        p220_sql = f"{p220:.4f}" if p220 is not None else "NULL"
                        rev_sql = int(raw_rev) if raw_rev is not None else "NULL"

                        raw_mkt_name = sec.get("mkt_name")
                        raw_codesub_nm = sec.get("codesub_nm")
                        fb_market_sql = f"'{str(raw_mkt_name).strip().replace("'", "''")}'" if raw_mkt_name else "NULL"
                        fb_exchange_sql = f"'{str(raw_codesub_nm).strip().replace("'", "''")}'" if raw_codesub_nm else "NULL"

                        sql_update_fb = f"""
                            UPDATE public.tickers SET 
                                current_price = {current_price_sql}, currency_id = '{raw_curr}', daily_turnover_usd = {vlt_sql},
                                signal_recommendation = '{recommendation}', signal_sma_100 = {p110_sql}, signal_price_to_sma100_pct = {price_to_sma100_pct_sql},
                                signal_sma_200 = {p220_sql}, signal_price_to_sma200_pct = {price_to_sma200_pct_sql},
                                signals_last_synced_at = CURRENT_TIMESTAMP, fb_market = {fb_market_sql}, fb_exchange = {fb_exchange_sql}
                            WHERE id = {t_id};
                        """

                        db_sys.execute_query(sql_update_fb)
                        total_updated += 1

                    # 🚨 ПЕРЕХВАТ 2: Ловим тихих зомби (кого ФБ выкинул из массива ответов из-за OTC/ITS шлюза)
                    for fb_code, node_map in fb_to_db_map.items():
                        if fb_code not in successfully_updated_fb_codes:
                            t_id, symbol = node_map["db_id"], node_map["symbol"]
                            logging.info(f"   ⚠️ [ФБ ИГНОР]: Брокер скрыл из ответа {symbol} ({fb_code}). Перенаправляю в Yahoo...")
                            yahoo_fallback_list.append({"db_id": t_id, "symbol": symbol})

                except Exception as batch_err:
                    logging.error(f"  ❌ Сбой Freedom-пачки в эшелоне №{echelon_idx}: {batch_err}")

            # =======================================================================================================
            # 🔥 ПОТОК Б: ВСТРОЕННЫЙ РЕЗЕРВНЫЙ КОНТУР YAHOO FINANCE (Для сирот и американских отказников)
            # =======================================================================================================
            if yahoo_fallback_list:
                logging.info(f"   📡 [FALLBACK YAHOO]: Эшелон №{echelon_idx} запускает сбор через yfinance для {len(yahoo_fallback_list)} акций...")
                for fallback_node in yahoo_fallback_list:
                    f_id, f_symbol = fallback_node["db_id"], fallback_node["symbol"]
                    
                    try:
                        res_yf_sym = db_sys.execute_query(f"SELECT yahoo_symbol, exchange_mic FROM public.tickers WHERE id = {f_id};")
                        if not res_yf_sym or len(res_yf_sym) == 0: continue
                        
                        row_yf = res_yf_sym[0]
                        yf_symbol = row_yf.get("yahoo_symbol")
                        f_mic = row_yf.get("exchange_mic")

                        if not f_mic:
                            logging.warning(f"   ⚠️ [FALLBACK СБОЙ]: Нет MIC в базе для {f_symbol}. Пропуск.")
                            continue

                        ticker_obj = yf.Ticker(yf_symbol)
                        hist = ticker_obj.history(period="1mo")
                        
                        if hist is not None and not hist.empty:
                            raw_yf_price = float(hist['Close'].tail(1).values[0])
                            raw_volume = float(hist['Volume'].tail(1).values[0]) if not hist['Volume'].empty else 0.0
                            
                            sql_ex_meta = f"SELECT currency_id FROM public.exchanges WHERE mic = '{f_mic}' LIMIT 1;"
                            res_ex_meta = db_sys.execute_query(sql_ex_meta)
                            y_curr = str(res_ex_meta[0]["currency_id"]).strip().upper() if res_ex_meta and len(res_ex_meta) > 0 else "USD"
                            
                            sql_y_multiplier = f"SELECT multiplier FROM public.currencies WHERE id = '{y_curr}' LIMIT 1;"
                            res_y_mult = db_sys.execute_query(sql_y_multiplier)
                            y_multiplier = float(res_y_mult[0]["multiplier"]) if res_y_mult and len(res_y_mult) > 0 and res_y_mult[0].get("multiplier") else 1.0

                            # 🔥 ПРИМЕНЯЕМ МУЛЬТИПЛИКАТОР (0.01 для пенсов, 1.0 для долларов)
                            clean_price = raw_yf_price * y_multiplier
                            
                            y_rate = float(rates_cache.get(y_curr, 1.0))
                            calc_y_turnover_usd = (raw_volume * clean_price) * y_rate

                            sql_update_yhoo = f"""
                                UPDATE public.tickers SET 
                                    current_price = {clean_price:.4f}, 
                                    currency_id = '{y_curr}', 
                                    daily_turnover_usd = {calc_y_turnover_usd:.2f},
                                    signal_recommendation = 'NEUTRAL', 
                                    signal_sma_100 = NULL, 
                                    signal_price_to_sma100_pct = NULL,
                                    signal_sma_200 = NULL, 
                                    signal_price_to_sma200_pct = NULL, 
                                    signals_last_synced_at = CURRENT_TIMESTAMP, 
                                    fb_market = 'YAHOO_FALLBACK', 
                                    fb_exchange = 'YAHOO_FALLBACK'
                                WHERE id = {f_id};
                            """
                            db_sys.execute_query(sql_update_yhoo)
                            total_updated += 1
                            logging.info(f"   ✅ [FALLBACK УСПЕХ]: Цена для {f_symbol} обновлена через Yahoo: {clean_price:.2f} {y_curr}")
                            time.sleep(config.PAUSE_SIGNALS_SEC)
                        else:
                            logging.warning(f"   ⚠️ Yahoo не вернул историю для '{yf_symbol}'")
                    except Exception as yhoo_err:
                        logging.error(f"   🚨 Сбой Yahoo для {f_symbol}: {yhoo_err}")

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
