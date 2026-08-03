#!/usr/bin/env python3
import os
import sys
import time
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd
import numpy as np

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули базы и утилит
sys.path.append(str(Path(__file__).parent.parent.resolve()))
from database import db_sys
import utils
import config
import settings

# Настраиваем логирование для контроля пакетного прогресса в реальном времени
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def sync_global_yahoo_signals(single_ticker_id=None):
    print("\n" + "="*120)
    print("🌅 [UPort GLOBAL YAHOO CONVEYER v1.0]: Глобальный пакетный экспресс-скоринг (100% Yahoo Finance)...")
    print("="*120)

    # 🔥 ШАГ 1: КЭШИРОВАНИЕ КУРСОВ ВАЛЮТ ДЛЯ ПЕРЕСЧЕТА ОБОР ТОВ В USD
    logging.info("💸 Загружаю актуальные курсы валют из public.currency_rates для пересчета оборотов в USD...")
    sql_rates = "SELECT from_currency, rate FROM public.currency_rates WHERE to_currency = 'USD';"
    db_rates = db_sys.execute_query(sql_rates)
    rates_cache = {str(r["from_currency"]).upper().strip(): float(r["rate"]) for r in db_rates if r.get("rate")} if db_rates else {}
    rates_cache["USD"] = 1.0  # Базовый доллар всегда равен единице

    # 🔥 ШАГ 2: SQL-ЗАПРОС НА ВЕСЬ UNIVERSE ТИКЕРОВ (С ПОДДЕРЖКОЙ ТОЧЕЧНОЙ ОТЛАДКИ)
    if single_ticker_id:
        logging.info(f"🎯 [РЕЖИМ ОДИНОЧНОГО ТИКЕРА]: Сбор котировок изолирован строго под ticker_id: {single_ticker_id}")
        sql_get_universe = f"SELECT id, symbol, (ticker_name_map->>'YAHOO') AS yahoo_symbol, exchange_mic FROM public.tickers WHERE id = {int(single_ticker_id)};"
    else:
        logging.info("📡 Запрашиваю полный Universe тикеров (S&P 500, S&P 400, FTSE 100) из СУБД...")
        # Выбираем только те боевые строки, которые прошли нашу генеральную чистку и имеют паспорт паспортистки
        sql_get_universe = """
            SELECT id, symbol, yahoo_symbol, exchange_mic FROM public.tickers 
            WHERE provenance::text LIKE '%"MS_%' 
               OR provenance::text LIKE '%TG_USR_ID=%'
               OR provenance::text LIKE '%LST_ID=%';
        """
            
    active_rows = db_sys.execute_query(sql_get_universe)
    if not active_rows:
        logging.warning("⚠️ Инструментов для обработки в Universe СУБД не обнаружено.")
        return {"processed": 0, "errors": 0}

    total_tickers = len(active_rows)
    logging.info(f"📊 В обойму глобального конвейера зашло: {total_tickers} акций. Запуск пакетных эшелонов...")
    total_updated = 0

    with utils.timer("Глобальный технический скоринг Yahoo UPort"):
        # Нарезаем 940 акций на эшелоны по 50 штук с помощью вашего родного Chunker
        for echelon_idx, db_batch in utils.create_echelons(active_rows, chunk_size=config.CHUNK_SIZE_SIGNALS):

            logging.info(f"\n===== 📦 ОБРАБОТКА ЭШЕЛОНА {echelon_idx} (Пачка: {len(db_batch)} акций) =====")
            
            # 🤝 СУПЕРЧАНК ДЛЯ YAHOO: Собираем список yahoo_symbol для пакетного запроса
            yahoo_symbols_list = []
            db_map = {}  # Карта для сопоставления ответов Yahoo с ID нашей базы данных
            
            for row in db_batch:
                y_sym = row.get("yahoo_symbol") or row["symbol"]
                yahoo_symbols_list.append(y_sym)
                db_map[y_sym] = row

            yahoo_batch_string = " ".join(yahoo_symbols_list)
            
            try:
                logging.info(f"   🌐 [YAHOO SUPERCHUNK]: Скачиваю годовую историю (1y) сразу для {len(db_batch)} бумаг...")
                # Скачиваем годовую историю одним сетевым прыжком для всей пачки
                batch_data = yf.Tickers(yahoo_batch_string).history(period="1y")
                logging.info("   ✅ Пачка исторических данных успешно загружена в память.")
            except Exception as batch_err:
                logging.error(f"   🚨 Ошибка пакетной загрузки Yahoo для эшелона {echelon_idx}: {batch_err}")
                continue

            # 🔄 ПОТОКОВАЯ ОБРАБОТКА И МАТЕМАТИЧЕСКИЙ СКОРИНГ
            for yf_symbol in yahoo_symbols_list:
                node = db_map[yf_symbol]
                db_id, symbol, f_mic = node["id"], node["symbol"], node.get("exchange_mic")
                
                try:
                    # 🔥 ИСПРАВЛЕНО НАМЕРТВО: Универсальное извлечение данных из MultiIndex yfinance.
                    # Позволяет одинаково успешно читать историю как в пакетном режиме, так и для одиночной отладки.
                    if isinstance(batch_data.columns, pd.MultiIndex):
                        # Безопасно вырезаем срез для конкретного тикера по второму уровню колонок (level=1)
                        if yf_symbol in batch_data.columns.get_level_values(1):
                            hist = batch_data.xs(yf_symbol, axis=1, level=1).copy()
                        else:
                            continue
                    else:
                        # Если прилетела обычная плоская таблица (резервный случай)
                        hist = batch_data.copy()

                    if hist is None or hist.empty:
                        continue

                    # Намертво вырезаем пустые строки, если они просочились
                    hist = hist.dropna(subset=['Close'])


                    if hist is None or hist.empty:
                        continue

                    # Удаляем пустые строки (выходные дни), если они просочились
                    hist = hist.dropna(subset=['Close'])
                    total_days = len(hist)

                    if total_days < 200:
                        # Защитный барьер: если история слишком короткая, пропускаем расчет
                        continue

                    # Находим базовую текущую цену (самая свежая ячейка Close)
                    raw_yf_price = float(hist['Close'].iloc[-1])
                    raw_volume = float(hist['Volume'].iloc[-1]) if not hist['Volume'].empty else 0.0

                    # 💸 ПОДТЯГИВАЕМ МЕТАДАННЫЕ ВАЛЮТЫ И НАШ СКВОЗНОЙ МУЛЬТИПЛИКАТОР
                    sql_ex_meta = f"SELECT currency_id FROM public.exchanges WHERE mic = '{f_mic}' LIMIT 1;"
                    res_ex_meta = db_sys.execute_query(sql_ex_meta)
                    y_curr = str(res_ex_meta[0]["currency_id"]).strip().upper() if res_ex_meta and len(res_ex_meta) > 0 else "USD"
                    
                    sql_y_multiplier = f"SELECT multiplier FROM public.currencies WHERE id = '{y_curr}' LIMIT 1;"
                    res_y_mult = db_sys.execute_query(sql_y_multiplier)
                    y_multiplier = float(res_y_mult[0]["multiplier"]) if res_y_mult and len(res_y_mult) > 0 and res_y_mult[0].get("multiplier") else 1.0

                    # 🔥 НАШ УНИВЕРСАЛЬНЫЙ СКВОЗНОЙ ПАТТЕРН: Применяем мультипликатор к цене
                    # Лондонские пенсы превратятся в фунты, а доллары и тенге умножатся на 1.0
                    clean_price = raw_yf_price * y_multiplier
                    
                    # Пересчитываем дневной оборот торгов в доллары США через rates_cache
                    y_rate = float(rates_cache.get(y_curr, 1.0))
                    calc_y_turnover_usd = (raw_volume * clean_price) * y_rate

                    # 🧮 ВЕКТОРНЫЙ РАСЧЕТ ИНДИКАТОРОВ PANDAS (СБРОС ИНДЕКСОВ ДЛЯ ЗАЩИТЫ ПАМЯТИ)
                    history_series = pd.Series(hist['Close'].values).reset_index(drop=True)

                    ema_20_series = history_series.ewm(span=20, adjust=False).mean()
                    ema_20 = float(ema_20_series.iloc[-1])
                    sma_50 = float(history_series.tail(50).mean())
                    sma_100 = float(history_series.tail(100).mean())
                    sma_200 = float(history_series.tail(200).mean())

                    # Блок 2 "Поведение" body-стандарта карточки тикера (см.
                    # Claude/05_strategy_screen_and_kubiki.md, Claude/07_glossary.md): простое
                    # % изменение цены закрытия за 1д/1нед/1мес/1год -- из той же истории, что
                    # и SMA/RSI/MACD выше, а не отдельным сетевым запросом. Индексы -- назад от
                    # последней точки (iloc[-1] == raw_yf_price): 1 торговый день/неделя/месяц;
                    # "1 год" -- от самой старой точки скачанного окна (iloc[0]), не строго 365 дней.
                    pct_1d = ((raw_yf_price / float(history_series.iloc[-2])) - 1) * 100 if total_days >= 2 else 0.0
                    pct_1w = ((raw_yf_price / float(history_series.iloc[-6])) - 1) * 100 if total_days >= 6 else 0.0
                    pct_1m = ((raw_yf_price / float(history_series.iloc[-22])) - 1) * 100 if total_days >= 22 else 0.0
                    pct_1y = ((raw_yf_price / float(history_series.iloc[0])) - 1) * 100

                    # Расчет процентов отклонения
                    price_to_sma100_pct = ((raw_yf_price / sma_100) - 1) * 100
                    price_to_sma200_pct = ((raw_yf_price / sma_200) - 1) * 100
                    
                    # Расчет RSI-14 по методу Уайлдера
                    delta = history_series.diff()
                    gain = (delta.where(delta > 0, 0)).copy()
                    loss = (-delta.where(delta < 0, 0)).copy()
                    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
                    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
                    
                    rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
                    rsi_series = 100 - (100 / (1 + rs))
                    rsi_14 = float(rsi_series.iloc[-1])
                    
                    # Расчет Линии MACD (EMA_12 - EMA_26)
                    ema_12 = history_series.ewm(span=12, adjust=False).mean()
                    ema_26 = history_series.ewm(span=26, adjust=False).mean()
                    macd_line = ema_12 - ema_26
                    macd_val = float(macd_line.iloc[-1])

                    # Дневная волатильность -- общий примитив для PriceMoveWatcher и
                    # протухания приказов/алертов (см. Claude/BACKLOG.md, 2026-07-30):
                    # std дневных % изменений цены за окно, из той же истории, что и
                    # остальные сигналы выше -- множитель валюты не нужен (отношение).
                    daily_returns = history_series.pct_change().dropna()
                    if len(daily_returns) >= settings.DAILY_VOLATILITY_WINDOW_DAYS:
                        daily_volatility_pct = float(daily_returns.tail(settings.DAILY_VOLATILITY_WINDOW_DAYS).std() * 100)
                    else:
                        daily_volatility_pct = None

                    # Подтверждённый слом тренда (settings.TREND_REVERSAL_CONFIRM_DAYS,
                    # см. Claude/12_investment_goal_and_mechanisms_roadmap.md) -- знаковая длина
                    # серии закрытий по одну сторону от EMA20, из той же ema_20_series, что и
                    # снимок EMA20 выше -- отдельной истории хранить не нужно.
                    above_ema = (history_series > ema_20_series).values
                    streak_days = 1
                    for is_above in above_ema[-2::-1]:
                        if is_above == above_ema[-1]:
                            streak_days += 1
                        else:
                            break
                    ema20_streak_days = streak_days if above_ema[-1] else -streak_days

                    # Долгосрочный ценовой тренд (позиция цены относительно SMA100/SMA200) --
                    # НЕ инвестиционная рекомендация конкретной стратегии (та считается отдельно
                    # в analytics/analytics_utils.py). Значения намеренно не BUY/SELL, чтобы не
                    # путать с реальными сигналами стратегий на этом же экране (см. BACKLOG.md).
                    if raw_yf_price > sma_200 and raw_yf_price > sma_100:
                        recommendation = "UPTREND"
                    elif raw_yf_price < sma_200 and raw_yf_price < sma_100:
                        recommendation = "DOWNTREND"
                    else:
                        recommendation = "NEUTRAL"

                    # Применяем мультипликатор к индикаторам для синхронизации с clean_price базы
                    ema20_sql = f"{ema_20 * y_multiplier:.4f}"
                    sma50_sql = f"{sma_50 * y_multiplier:.4f}"
                    sma100_sql = f"{sma_100 * y_multiplier:.4f}"
                    sma200_sql = f"{sma_200 * y_multiplier:.4f}"
                    pct100_sql = f"{price_to_sma100_pct:.2f}"
                    pct200_sql = f"{price_to_sma200_pct:.2f}"
                    rsi_sql = f"{rsi_14:.2f}"
                    macd_sql = f"{macd_val * y_multiplier:.4f}"
                    # Проценты изменения цены -- отношение, множитель валюты сокращается
                    # (числитель и знаменатель в одних и тех же единицах), не применяем
                    pct1d_sql = f"{pct_1d:.2f}"
                    pct1w_sql = f"{pct_1w:.2f}"
                    pct1m_sql = f"{pct_1m:.2f}"
                    pct1y_sql = f"{pct_1y:.2f}"
                    volatility_sql = f"{daily_volatility_pct:.4f}" if daily_volatility_pct is not None else "NULL"
                    streak_sql = f"{ema20_streak_days}"

                    # 🔥 АТОМАРНЫЙ UPDATE СТРОКИ В СУБД (БЕЗ ЗАТИРАНИЯ ДАТЫ ОТЧЕТОВ)
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
                            signal_pct_1d = {pct1d_sql},
                            signal_pct_1w = {pct1w_sql},
                            signal_pct_1m = {pct1m_sql},
                            signal_pct_1y = {pct1y_sql},
                            signal_daily_volatility_pct = {volatility_sql},
                            signal_ema20_streak_days = {streak_sql},
                            signals_last_synced_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                            fb_market = 'YAHOO_GLOBAL', 
                            fb_exchange = 'YAHOO_GLOBAL'
                        WHERE id = {db_id};
                    """
                    db_sys.execute_query(sql_update_yhoo)
                    total_updated += 1

                except Exception as single_err:
                    logging.error(f"   🚨 Сбой расчета индикаторов Yahoo для '{symbol}': {single_err}")

            # ⏳ БЕЗОПАСНАЯ ПАУЗА МЕЖДУ ЭШЕЛОНАМИ ДЛЯ ЗАЩИТЫ RATE LIMIT
            time.sleep(config.PAUSE_SIGNALS_SEC)
            logging.info(f"   ✅ [ЭШЕЛОН №{echelon_idx} ЗАВЕРШЕН]: Накопительный итог: {total_updated} шт.")

    print("\n" + "="*120)
    print(f"🏁 [GLOBAL YAHOO CONVEYER COMPLETE]: Успешно наполнено: {total_updated} из {total_tickers} акций!")
    print("="*120 + "\n")

    # Честная статистика выполнения (Claude/BACKLOG.md, п.25, 2026-08-03) -- errors считает
    # ЛЮБУЮ причину незавершения (сбой пакетной загрузки эшелона, сбой расчёта по одному
    # тикеру, пропуск из-за короткой истории) как единое "не обновилось", не различая причину.
    return {"processed": total_tickers, "errors": total_tickers - total_updated}

if __name__ == "__main__":
    # Запуск глобального экспресс-конвейера
    sync_global_yahoo_signals()
