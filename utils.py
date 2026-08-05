# /root/UPort/utils.py
#!/usr/bin/env python3
import logging
import time
import json
from contextlib import contextmanager
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Глобальный кэш суффиксов в оперативной памяти сервера UPort
_EXCHANGE_SUFFIXES_CACHE = None

# !!!! Старый вариант! Не использовать!
def to_yahoo_symbol(db_instance, symbol: str) -> str:
    """
    ГЛОБАЛЬНЫЙ ТРАНСЛЯТОР ТИКЕРОВ UPort v2.0.
    Преобразует символ Freedom Broker в формат Yahoo Finance через ваш REST-шлюз.
    """
    global _EXCHANGE_SUFFIXES_CACHE
    
    clean_sym = str(symbol).strip()
    if not clean_sym:
        return ""
        
    if _EXCHANGE_SUFFIXES_CACHE is None:
        sql_suffixes = "SELECT yahoo_suffix FROM public.exchanges WHERE yahoo_suffix != '';"
        rows = db_instance.execute_query(sql_suffixes)
        
        if rows and isinstance(rows, list):
            _EXCHANGE_SUFFIXES_CACHE = tuple(row["yahoo_suffix"] for row in rows if "yahoo_suffix" in row)
            logging.info(f"💾 [UTILS CACHE]: Кэш международных суффиксов успешно взведен: {_EXCHANGE_SUFFIXES_CACHE}")
        else:
            _EXCHANGE_SUFFIXES_CACHE = ('.L', '.DE', '.KS', '.ME')
            logging.warning(f"⚠️ [UTILS СБОЙ ШЛЮЗА]: Применен аварийный щит суффиксов: {rows}")

    if clean_sym.endswith(_EXCHANGE_SUFFIXES_CACHE):
        return clean_sym  
    else:
        return clean_sym.replace('.', '-')  

# !!!! Старый вариант! Не использовать!
def detect_ticker_passport(db_instance, symbol: str) -> dict:
    """
    ЭЛИТНЫЙ РАЗВЕДЧИК ПАСПОРТОВ UPort v3.5.
    """
    clean_sym = str(symbol).strip()
    if not clean_sym:
        return None
        
    try:
        yahoo_symbol = to_yahoo_symbol(db_instance, clean_sym)
        ticker_obj = yf.Ticker(yahoo_symbol)
        yf_exchange_code = ticker_obj.fast_info.get("exchange")
        
        if not yf_exchange_code or yf_exchange_code == "Unknown":
            logging.warning(f"⚠️ [PASSPORT DETECTOR]: Yahoo Finance не вернул код биржи для {yahoo_symbol}")
            return None
            
        sql_lookup = "SELECT mic, currency_id FROM public.exchanges WHERE yahoo_code = %s LIMIT 1;"
        db_rows = db_instance.execute_query(sql_lookup, (yf_exchange_code,))
        
        if db_rows and isinstance(db_rows, list) and len(db_rows) > 0:
            match = db_rows[0] 
            return {
                "mic": match["mic"],
                "currency_id": match["currency_id"],  
                "yahoo_symbol": yahoo_symbol
            }
        else:
            logging.warning(f"⚠️ [PASSPORT DETECTOR]: Код Yahoo '{yf_exchange_code}' ({yahoo_symbol}) пока отсутствует in public.exchanges!")
            return None
            
    except Exception as err:
        logging.error(f"❌ [PASSPORT DETECTOR КРИТИЧЕСКИЙ СБОЙ] для {symbol}: {err}")
        return None


def chunk_array(array_data: list, chunk_size: int = 100):
    """ПОТОКОВЫЙ НАРЕЗЧИК МАССИВОВ (Старый Chunker для обратной совместимости)."""
    for i in range(0, len(array_data), chunk_size):
        yield array_data[i:i + chunk_size]


def create_echelons(data_list: list, chunk_size: int):
    """
    УНИВЕРСАЛЬНЫЙ ОРГАНИЗАТОР ЭШЕЛОНОВ UPort v4.0.
    Нарезает входящий массив на пачки переменного размера со встроенным 
    прозрачным логированием шагов для VSCode.
    """
    if not data_list:
        logging.warning("⚠️ [UPort CHUNKER]: На вход подан пустой массив данных.")
        return
        
    if chunk_size <= 0:
        logging.error(f"❌ [UPort CHUNKER]: Некорректный размер эшелона ({chunk_size}). Сброс на размер по умолчанию (50).")
        chunk_size = 50

    total_items = len(data_list)
    echelon_idx = 0
    
    for i in range(0, total_items, chunk_size):
        echelon_idx += 1
        batch = data_list[i:i + chunk_size]
        logging.info(f"   📦 [ЭШЕЛОН №{echelon_idx}]: Сформирована пачка из {len(batch)} элементов (Всего обработано: {min(i + chunk_size, total_items)}/{total_items}).")
        yield echelon_idx, batch


@contextmanager
def timer(process_name: str):
    """ИНФРАСТРУКТУРНЫЙ ПРОФАЙЛЕР ВРЕМЕНИ (Profiler)."""
    start_time = time.time()
    yield
    elapsed = time.time() - start_time
    logging.info(f"⏱️ [ТАЙМЕР]: Процесс '{process_name}' успешно выполнен за {elapsed:.2f} сек.")

def ensure_ticker_passport_in_db(db_instance, raw_string: str, fb_client) -> int:
    """
    ГЛОБАЛЬНЫЙ ВХОДНОЙ ШЛЮЗ UPort v7.2 (МОНОЛИТНАЯ ПАСПОРТИЗАЦИЯ И СЕГРЕГАЦИЯ).
    Принимает любую строку, послойно очищает шелуху брокеров, защищает классы акций,
    сохраняет суффиксы для международных бирж в поле symbol, генерирует карту имен
    и атомарно сохраняет эталонный паспорт в public.tickers, возвращая ticker_id.
    """
    # 🛡️ СЛОЙ В (КОНСТАНТЫ): Мировые расширения классов акций, защищенные от отрезания.
    # Точки перед этими буквами система никогда не тронет, так как это часть тела тикера.
    BEHIND_THE_DOT_EXTENSIONS = {
        '.A', '.B', '.C', '.K', '.F',  # Разные классы обыкновенных акций (права голоса)
        '.P', '.PR',                  # Привилегированные акции (Preferred)
        '.U', '.UN', '.UT',            # Инвестиционные юниты и траст-пакеты (SPAC)
        '.W', '.WT', '.R', '.RT',      # Варранты и Права (Warrants / Rights)
    }

    # Первичная очистка входящей строки от случайных пробелов
    clean_sym = str(raw_string).strip()
    if not clean_sym:
        logging.error("❌ [UPort GATEWAY]: На вход подан пустой символ.")
        return None

    logging.info(f"🔮 [UPort GATEWAY START]: Начинаю сквозную прописку для строки: '{clean_sym}'")
    
    target_mic = None
    target_exchange_code = None
    target_currency_id = None
    target_report_date = None 
    report_date_sql = "NULL" 
    ticker_name_map = {}
    
    # 🚀 СТАРТ ЦИКЛА ПОСЛОЙНОЙ ОЧИСТКИ ХВОСТОВ (Разбираем строку справа налево)
    while '.' in clean_sym:
        sym_upper = clean_sym.upper()
        
        # 🎯 СЛОЙ А: ЗРЯЧИЙ ПЕРЕХВАТ РЫНКОВ БРОКЕРА (Снятие шелухи строго по фото терминала)
        # Если находим маркер брокера — срезаем его и сразу жестко фиксируем мировую биржу (MIC)
        if sym_upper.endswith(".US"):
            clean_sym = clean_sym[:-3]
            target_mic = "XNAS"  # Американский пул FIX
            logging.info(f"   [GATEWAY]: Снят слой брокера '.US'. Тело: '{clean_sym}', Фиксация MIC: {target_mic}")
            continue
        elif sym_upper.endswith(".EU"):
            clean_sym = clean_sym[:-3]
            target_mic = "XLON"  # Лондон/Европа
            logging.info(f"   [GATEWAY]: Снят слой брокера '.EU'. Тело: '{clean_sym}', Фиксация MIC: {target_mic}")
            continue
        elif sym_upper.endswith(".LN"):  # Альтернативный перехват профессионального кода Лондона
            clean_sym = clean_sym[:-3]
            target_mic = "XLON"
            logging.info(f"   [GATEWAY]: Перехвачен маркер '.LN'. Отрезан. Фиксация MIC: {target_mic}")
            continue
        elif sym_upper.endswith(".AIX.KZ"):
            clean_sym = clean_sym[:-7]
            target_mic = "XAST"  # Казахстан, биржа Астаны (AIX)
            logging.info(f"   [GATEWAY]: Снят слой брокера '.AIX.KZ'. Тело: '{clean_sym}', Фиксация MIC: {target_mic}")
            continue
        elif sym_upper.endswith(".KZ"):
            clean_sym = clean_sym[:-3]
            target_mic = "XKAS"  # Казахстан, биржа Алматы (KASE)
            logging.info(f"   [GATEWAY]: Снят слой брокера '.KZ'. Тело: '{clean_sym}', Фиксация MIC: {target_mic}")
            continue
            
        # Вычисляем текущий крайний правый суффикс для Слоев Б и В (например, '.L' из 'ULVR.L' или '.B' из 'BRK.B')
        parts = clean_sym.upper().rsplit('.', 1)
        current_tail = f".{parts[1]}"
        
        # 🎯 СЛОЙ Б: ДИНАМИЧЕСКАЯ ПРОВЕРКА ПО БАЗЕ ДАННЫХ — ЭТО ОФИЦИАЛЬНЫЙ СУФФИКС BIRZHI (.L)?
        sql_yahoo_suffixes = "SELECT mic, exchange_code, yahoo_suffix, currency_id FROM public.exchanges WHERE yahoo_suffix IS NOT NULL AND yahoo_suffix != '';"
        db_yahoo_suffixes = db_instance.execute_query(sql_yahoo_suffixes)
        
        is_legal_exchange_suffix = False
        if db_yahoo_suffixes and isinstance(db_yahoo_suffixes, list):
            for row in db_yahoo_suffixes:
                sfx = str(row["yahoo_suffix"]).strip().upper()
                if current_tail == sfx:
                    target_mic = row["mic"]
                    target_exchange_code = row["exchange_code"]
                    target_currency_id = row["currency_id"]
                    clean_sym = clean_sym[:-len(sfx)]  # Отрезаем суффикс биржи для получения чистого базового тела
                    is_legal_exchange_suffix = True
                    logging.info(f"   [GATEWAY]: Снят суффикс биржи '{sfx}'. Тело: '{clean_sym}', MIC: {target_mic}")
                    break
                    
        # Если это легальный суффикс биржи (как .L), география определена на 100% — прерываем цикл очистки
        if is_legal_exchange_suffix:
            break
            
        # 🎯 СЛОЙ В: ЗАЩИТНЫЙ ЩИТ КЛАССОВ АКЦИЙ США (Защищаем 'BRK.B' от отрезания)
        if current_tail in BEHIND_THE_DOT_EXTENSIONS:
            logging.info(f"   [GATEWAY]: Хвост '{current_tail}' защищен как класс акций. Очистка завершена.")
            break
            
        # Аварийный сброс неизвестной технической шелухи, если она проскочила все три слоя защиты
        clean_sym = clean_sym[:-len(current_tail)]
        logging.info(f"   [GATEWAY]: Неизвестный маркер '{current_tail}' отброшен. Осталось: '{clean_sym}'")

    # Вытаскиваем чистое эталонное тело тикера в верхнем регистре (например, AAPL, BRK.B, BP, CCBN)
    body_upper = clean_sym.upper()

    # 🔥 ИНТЕЛЛЕКТУАЛЬНАЯ СБОРКА ИТОГОВОГО СИСТЕМНОГО ИМЕНИ (symbol_uport) ДЛЯ ЗАЩИТЫ ОТ ДУБЛИКАТОВ
    # Если бумага международная — мы обязаны сохранить её биржевой суффикс внутри symbol, чтобы развести строки
    if target_mic:
        sql_get_sfx = "SELECT yahoo_suffix, currency_id FROM public.exchanges WHERE mic = %s LIMIT 1;"
        res_sfx = db_instance.execute_query(sql_get_sfx, (target_mic,))
        
        # ЖЕЛЕЗОБЕТОННО: Извлекаем первую строку [0] из списка ответов базы данных
        if res_sfx and isinstance(res_sfx, list) and len(res_sfx) > 0:
            first_row = res_sfx[0]
            yahoo_suffix_str = str(first_row.get("yahoo_suffix", "")).strip().upper()
            if not target_currency_id:
                target_currency_id = first_row.get("currency_id")
            
            # Если суффикс у биржи в СУБД прописан (как .L или .KZ), склеиваем его с symbol!
            if yahoo_suffix_str and yahoo_suffix_str != "":
                symbol_uport = f"{body_upper}{yahoo_suffix_str}"  # В базу запишется: BP.L или CCBN.KZ
            else:
                symbol_uport = body_upper  # Для США (где суффикс в базе пустой) symbol останется чистым: BP
                
            yahoo_symbol_request = f"{body_upper}{yahoo_suffix_str}"
        else:
            symbol_uport = body_upper
            yahoo_symbol_request = body_upper
    else:
        # Сюда залетают американские классы без MIC на старте (BRK.B) — меняем точку на дефис для Yahoo
        symbol_uport = body_upper
        yahoo_symbol_request = body_upper.replace('.', '-')

    # 🎯 ШАГ 3: РЕЗЕРВНАЯ СЕТЕВАЯ РАЗВЕДКА YAHOO FINANCE, СБОР ТИПА И КОНТРОЛЬ ДЕЛИСТИНГА
    if not target_mic:
        try:
            logging.info(f"   [GATEWAY]: Сетевой поиск в Yahoo Finance для '{yahoo_symbol_request}'...")
            ticker_obj = yf.Ticker(yahoo_symbol_request)
            
            # Попытка №1: Извлекаем код биржи и тип актива через быстрый fast_info контур
            yf_exchange_code = ticker_obj.fast_info.get("exchange")
            
            # 🔥 БОЕВОЙ ПРЕДОХРАНИТЕЛЬ: Если биржа пустая или неизвестная — проверяем историю на делистинг
            if not yf_exchange_code or str(yf_exchange_code).strip().upper() in ("UNKNOWN", ""):
                h_test = ticker_obj.history(period="1d")
                if h_test is None or h_test.empty:
                    raise ValueError("Этот инструмент не найден на мировых биржах")
                
                # Если история выдала структуру, проверяем древность последней сделки
                last_date = h_test.index[-1]
                if hasattr(last_date, 'to_pydatetime'):
                    last_date = last_date.to_pydatetime().replace(tzinfo=None)
                
                import pandas as pd
                days_delta = (pd.Timestamp.now().replace(tzinfo=None) - last_date).days
                if days_delta > 7:
                    # Торгов на бирже нет больше недели — это мертвый делистинг, блокируем!
                    raise ValueError("Этот инструмент не найден на мировых биржах")
                
                # Если проверка времени пройдена, принудительно восстанавливаем код биржи (например для SATS)
                yf_exchange_code = "UNKNOWN"

            # Ищем MIC в СУБД по коду ответа от Yahoo Finance
            sql_lookup_mic = """
                SELECT mic, exchange_code, currency_id FROM public.exchanges
                WHERE yahoo_code = %s OR exchange_code = %s
                ORDER BY (mic = 'XNAS' OR mic = 'XNYS') DESC LIMIT 1;
            """
            match_rows = db_instance.execute_query(sql_lookup_mic, (yf_exchange_code, yf_exchange_code))
            if match_rows and isinstance(match_rows, list) and len(match_rows) > 0:
                target_mic = match_rows[0]["mic"]
                target_exchange_code = match_rows[0]["exchange_code"]
                target_currency_id = match_rows[0]["currency_id"]
                logging.info(f"   [GATEWAY]: Код сопоставлен. Выдан MIC: {target_mic}")
                
                sql_recheck = "SELECT yahoo_suffix FROM public.exchanges WHERE mic = %s LIMIT 1;"
                res_recheck = db_instance.execute_query(sql_recheck, (target_mic,))
                if res_recheck and isinstance(res_recheck, list) and len(res_recheck) > 0:
                    sfx_str = str(res_recheck[0].get("yahoo_suffix", "")).strip().upper()
                    if sfx_str and sfx_str != "":
                        symbol_uport = f"{body_upper}{sfx_str}"
            else:
                # Если биржа вернулась, но она не поддерживается нашей СУБД — ставим шлагбаум
                raise ValueError("Этот инструмент торгуется на неподдерживаемой мировой бирже")

            # Безопасный сбор даты отчета по сети
            cal = ticker_obj.calendar
            if isinstance(cal, dict) and 'Earnings Date' in cal and cal['Earnings Date']:
                earnings_dates = cal['Earnings Date']
                if earnings_dates and isinstance(earnings_dates, list) and len(earnings_dates) > 0:
                    first_date_obj = earnings_dates[0]
                    if hasattr(first_date_obj, 'strftime'):
                        target_report_date = str(first_date_obj.strftime('%Y-%m-%d')).strip()

        except Exception as network_err:
            # Пробрасываем наши осознанные ValueError наверх в ТГ-бот без изменений
            if isinstance(network_err, ValueError):
                raise network_err
                                
            logging.error(f"   🚨 [GATEWAY СБОЙ ПРЕДОХРАНИТЕЛЯ YAHOO]: {network_err}")
            raise ValueError("Этот инструмент не найден на мировых биржах")

    else:
        # 🔥 ЕСЛИ МИК ОПРЕДЕЛЕН ИЗ БАЗЫ БЕЗ СЕТИ (Например, AAPL.US или ULVR.L)
        # Мы все равно быстро дергаем календарь Yahoo, чтобы забрать дату отчета компании
        try:
            ticker_obj = yf.Ticker(yahoo_symbol_request)
            cal = ticker_obj.calendar
            if isinstance(cal, dict) and 'Earnings Date' in cal and cal['Earnings Date']:
                earnings_dates = cal['Earnings Date']
                if earnings_dates and isinstance(earnings_dates, list) and len(earnings_dates) > 0:
                    first_date_obj = earnings_dates[0]
                    if hasattr(first_date_obj, 'strftime'):
                        target_report_date = str(first_date_obj.strftime('%Y-%m-%d')).strip()
        except:
            pass # Если у европейских ETF/фондов дат нет, просто молча идем дальше

    # Окончательно фиксируем эталонный ключ для запросов к Yahoo в нашей карте имен
    ticker_name_map["YAHOO"] = yahoo_symbol_request

    # ─── ШАГ 4: СКВОЗНОЙ ЛИНЕЙНЫЙ СБОР ТИПА АКТИВА YAHOO (ОБЯЗАТЕЛЕН ДЛЯ ВСЕХ) ───
    yf_asset_type = "UNDETERMINED"
    try:
        # Инициализируем сетевой объект для гарантированного сбора типа (для ТГ и Брокера)
        ticker_obj = yf.Ticker(yahoo_symbol_request)
        raw_type = ticker_obj.fast_info.get("quoteType")
        if raw_type:
            yf_asset_type = str(raw_type).strip().upper()
    except Exception as t_type_err:
        # Если fast_info выдал технический сбой, страхуем оригинальным маркером неизвестности
        logging.warning(f"   ⚠️ [GATEWAY]: Не удалось извлечь quoteType для '{yahoo_symbol_request}': {t_type_err}")
        yf_asset_type = "UNDETERMINED"

    # =======================================================================================================
    # 🎯 ЧАСТЬ 2: РАБОТА С БРОКЕРАМИ И АТОМАРНАЯ ЗАПИСЬ ПАСПОРТА В СУБД
    # =======================================================================================================
    fb_ticker_result = "UNSUPPORTED"
    
    # Сегрегация на берегу: если биржа не из пула США/Казахстана, шлюз ФБ полностью пропускаем
    if target_mic and target_mic not in ["XNAS", "XNYS", "ARCX", "IEXG", "XNMS", "XNCM", "XKAS", "XAST"]:
        logging.info(f"   [GATEWAY - СЕГРЕГАЦИЯ]: Биржа {target_mic} вне зоны ФБ. Маркер UNSUPPORTED.")
    else:
        try:
            # Поиск в Freedom Broker всегда делаем по чистому БАЗОВОМУ телу (body_upper)
            logging.info(f"   [GATEWAY]: Поиск find_ticker('{body_upper}') в Freedom Broker...")
            found_data = fb_client.find_ticker(body_upper)
            if found_data and isinstance(found_data, list):
                for item in found_data:
                    exchange_ticker = str(item.get("code_nm", "")).strip().upper()
                    broker_market_code = str(item.get("mkt", "")).strip().upper()
                    
                    # Проверяем совпадение тела (до первой точки, защищая BRK.B)
                    if exchange_ticker == body_upper or ('.' in body_upper and exchange_ticker == body_upper.split('.')[0]):
                        sql_mkt_bridge = "SELECT uport_default_mic FROM public.fb_markets WHERE fb_market_code = %s LIMIT 1;"
                        mkt_bridge_rows = db_instance.execute_query(sql_mkt_bridge, (broker_market_code,))
                        
                        is_exchange_match = False
                        # ЖЕЛЕЗОБЕТОННО: Извлекаем строку из ответа моста рынков брокера
                        if mkt_bridge_rows and isinstance(mkt_bridge_rows, list) and len(mkt_bridge_rows) > 0:
                            bridge_mic = mkt_bridge_rows[0].get("uport_default_mic")
                            if bridge_mic and target_mic and bridge_mic.upper() == target_mic.upper():
                                is_exchange_match = True
                            elif broker_market_code == "FIX" and target_mic in ["XNAS", "XNYS", "ARCX", "IEXG", "XNMS", "XNCM"]:
                                is_exchange_match = True

                        if is_exchange_match:
                            fb_ticker_result = str(item.get("t", "")).strip().upper()
                            logging.info(f"   [GATEWAY]: Код ФБ успешно сопоставлен: '{fb_ticker_result}'")
                            break
        except Exception as fb_err:
            logging.error(f"   🚨 [GATEWAY СБОЙ ФБ ПЕРЕВОДЧИКА]: {fb_err}")

    # Записываем вычисленный ключ ФБ в карту имен
    ticker_name_map["FB"] = fb_ticker_result

    # ⏳ ВРЕМЯНКА TRADING 212: Одобряем рынки США и Великобритании (XLON) по географии MIC
    t212_ticker_result = "UNSUPPORTED"
    if target_mic and target_mic in ["XNAS", "XNYS", "ARCX", "IEXG", "XNMS", "XNCM", "XLON"]:
        t212_ticker_result = body_upper
    ticker_name_map["T212"] = t212_ticker_result

    # Значения NULL/строка передаются параметрами напрямую -- пред-цитированные
    # SQL-фрагменты ("'текст'"/"NULL" строкой) больше не нужны, psycopg2 сам
    # сериализует None в SQL NULL (см. Claude/BACKLOG.md №81).
    report_date_val = target_report_date or None
    fb_ticker_val = fb_ticker_result if fb_ticker_result != "UNSUPPORTED" else None
    mic_val = target_mic or None
    currency_val = target_currency_id or None
    json_map_str = json.dumps(ticker_name_map, ensure_ascii=False)

    # 🛡️ ПРЕДОХРАНИТЕЛЬ: Гарантируем наличие переменной yf_asset_type, если MIC определился без сети
    if 'yf_asset_type' not in locals():
        try:
            raw_type = ticker_obj.fast_info.get("quoteType") if 'ticker_obj' in locals() else None
            yf_asset_type = str(raw_type).strip().upper() if raw_type else "EQUITY"
        except:
            yf_asset_type = "EQUITY"

    # 🕵️‍♂️ ЗРЯЧАЯ ПРОВЕРКА КЭША СУБД: Истребляем холостую накрутку ID
    sql_check_exist = "SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;"
    res_exist = db_instance.execute_query(sql_check_exist, (symbol_uport,))

    # Стандарт времени: UTC с точностью до секунд без timezone
    system_now_sql = "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)"

    if res_exist and isinstance(res_exist, list) and len(res_exist) > 0:
        # --- ВЕТКА UPDATE: Паспорт уже существует, обновляем точечно по его ID ---
        row_t = res_exist[0] if isinstance(res_exist, list) else res_exist
        ticker_id = int(row_t["id"])

        sql_update_passport = f"""
            UPDATE public.tickers
            SET yahoo_symbol = %s,
                ticker_name_map = public.tickers.ticker_name_map || %s::jsonb,
                signal_next_report_date = %s,
                asset_type = %s,
                exchange_mic = CASE WHEN public.tickers.exchange_mic IS NULL OR public.tickers.exchange_mic = '' THEN %s ELSE public.tickers.exchange_mic END,
                currency_id = CASE WHEN public.tickers.currency_id IS NULL OR public.tickers.currency_id = '' THEN %s ELSE public.tickers.currency_id END,
                fb_ticker = CASE WHEN public.tickers.fb_ticker IS NULL OR public.tickers.fb_ticker = '' THEN %s ELSE public.tickers.fb_ticker END,
                last_updated_at = {system_now_sql}
            WHERE id = %s;
        """
        db_instance.execute_query(sql_update_passport, (
            yahoo_symbol_request, json_map_str, report_date_val, yf_asset_type,
            mic_val, currency_val, fb_ticker_val, ticker_id
        ))
        logging.info(f"📝 [PASSPORT]: Паспорт бумаги {symbol_uport} успешно актуализирован (ID: {ticker_id}).")
    else:
        # --- ВЕТКА INSERT: Новый уникальный паспорт в системе, берем чистый ID ---
        sql_insert_passport = f"""
            INSERT INTO public.tickers (
                symbol, yahoo_symbol, exchange_mic, currency_id, fb_ticker, ticker_name_map, signal_next_report_date, asset_type, last_updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, %s, %s, {system_now_sql}
            );
        """
        db_instance.execute_query(sql_insert_passport, (
            symbol_uport, yahoo_symbol_request, mic_val, currency_val, fb_ticker_val,
            json_map_str, report_date_val, yf_asset_type
        ))

        # Надежно забираем свежесгенерированный ID
        res_new_id = db_instance.execute_query("SELECT id FROM public.tickers WHERE symbol = %s LIMIT 1;", (symbol_uport,))
        row_n = res_new_id[0] if res_new_id and isinstance(res_new_id, list) and len(res_new_id) > 0 else (res_new_id if isinstance(res_new_id, dict) else {})
        ticker_id = int(row_n.get("id", 0))
        logging.info(f"➕ [PASSPORT]: Впервые создан эталонный паспорт для {symbol_uport} (ID: {ticker_id}).")

    if ticker_id > 0:
        logging.info(f"🏁 [UPort GATEWAY COMPLETE]: Паспорт для '{symbol_uport}' зафиксирован.")
        logging.debug(f"📌 [PASSPORT SUMMARY] {raw_string}: {json.dumps(ticker_name_map, ensure_ascii=False)}")
        return ticker_id

    return None


def manage_provenance(db_instance, ticker_id: int, source_key: str, action: str = "add") -> bool:
    """
    Универсальная служба управления историческими автографами provenance (JSONB) in public.tickers.
    """
    from datetime import timezone
    # 🔥 ИСПРАВЛЕНО UTC-СТАНДАРТ: Генерируем стерильное время по Гринвичу без часовых поясов и микросекунд.
    # Новые автографы зафиксируются в премиальном ровном виде: 'YYYY-MM-DD HH:MM:SS'
    current_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    
    action_clean = str(action).strip().lower()
    source_key_clean = str(source_key).strip()

    if action_clean == "add":
        # 🛡️ ЖЕЛЕЗОБЕТОННАЯ ЗАЩИТА СТАТИЧЕСКОЙ ДАТЫ:
        # С помощью оператора '?' проверяем, есть ли уже такой ключ в JSONB.
        # Если ЕСТЬ (CASE WHEN ... ? THEN) — оставляем старый provenance без изменений.
        # Если НЕТ — безопасно конкатенируем (||) новый ключ.
        sql_manage = """
            UPDATE public.tickers
            SET provenance = CASE
                WHEN COALESCE(provenance, '{}'::jsonb) ? %s THEN provenance
                ELSE COALESCE(provenance, '{}'::jsonb) || jsonb_build_object(%s, %s)
            END
            WHERE id = %s;
        """
        params = (source_key_clean, source_key_clean, current_timestamp, ticker_id)
        # На DEBUG (Claude/BACKLOG.md №28) -- вызывается на КАЖДЫЙ тикер при каждой синхронизации
        # алертов/вотчлиста, а SQL ниже и так no-op, если метка уже стоит (WHEN ... ? THEN provenance)
        logging.debug(f"💾 [PROVENANCE]: Попытка фиксации метки '{source_key_clean}' для ticker_id = {ticker_id}")

    elif action_clean == "remove":
        sql_manage = """
            UPDATE public.tickers
            SET provenance = COALESCE(provenance, '{}'::jsonb) - %s
            WHERE id = %s;
        """
        params = (source_key_clean, ticker_id)
        logging.debug(f"🧹 [PROVENANCE]: Команда удаления метки '{source_key_clean}' для ticker_id = {ticker_id}")

    else:
        logging.error(f"❌ [PROVENANCE ERROR]: Передана неподдерживаемая операция: '{action}'")
        return False

    try:
        db_instance.execute_query(sql_manage, params)
        return True
    except Exception as err:
        logging.error(f"🚨 [PROVENANCE CRITICAL СБОЙ СУБД]: Не удалось выполнить {action_clean} для {source_key_clean}: {err}")
        return False


def was_us_market_open_yesterday() -> bool:
    """
    Проверяет, торговался ли рынок США в последний завершившийся календарный день
    по нью-йоркскому времени -- через реальные исторические котировки SPY, а не
    простую проверку "будни/выходные" (так корректно ловит и праздники США, а не
    только субботу/воскресенье). Используется в cron_scheduler.py::digest_clock_loop,
    чтобы не слать дайджест повторно на тех же данных, если рынок не торговал.
    """
    try:
        ny_tz = ZoneInfo("America/New_York")
        yesterday_ny = (datetime.now(ny_tz) - timedelta(days=1)).date()

        history = yf.Ticker("SPY").history(period="5d")
        if history.empty:
            logging.warning("⚠️ [utils]: SPY не вернул историю котировок -- считаю, что торги были (безопасный дефолт).")
            return True

        last_trading_date = history.index[-1].date()
        return last_trading_date == yesterday_ny
    except Exception as e:
        logging.error(f"❌ [utils]: Ошибка проверки торгового дня через SPY: {e} -- считаю, что торги были (безопасный дефолт).")
        return True
