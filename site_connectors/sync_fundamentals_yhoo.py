# /root/UPort/site_connectors/sync_fundamentals_yhoo.py
#!/usr/bin/env python3
import os
import sys
import time
import json
import logging
from pathlib import Path
from datetime import datetime
import yfinance as yf

# Подгружаем системные пути ядра UPort, чтобы скрипт видел соседние модули
sys.path.append(str(Path(__file__).parent.parent.resolve()))

# ВНИМАНИЕ: Изменили импорт с settings на config, так как ядро UPort v10.0 использует config.py
import config

# 🔥 ИСПРАВЛЕНО: Добавили аргумент single_symbol для обработки одиночных новичков из ТГ в реальном времени
def sync_fundamentals(db_instance, single_ticker_id=None):
    """
    Контур В (UPort PRODUCTION v10.1): Ночное интеллектуальное обновление 18 фундаментальных показателей.
    Реализует концепцию 'Умного постотчетного триггера' и 'Размазывающего фонового щита' (LIMIT 20) через UNION.
    Использует извлечение yahoo_symbol напрямую из JSONB-картыticker_name_map.
    """
    
    # 🔥 ШАГ 1: ФОРМИРОВАНИЕ ИНТЕЛЛЕКТУАЛЬНОГО SQL-ЗАПРОСА С ДРОССЕЛИРОВАНИЕМ НАГРУЗКИ
    if single_ticker_id:
        logging.info(f"🎯 [ФУНДАМЕНТАЛ ШЛЮЗ]: Экстренный сбор показателей для одиночной новой бумаги с ticker_id: {single_ticker_id}")
        # Извлекаем yahoo_symbol из JSONB-карты по ключу "YAHOO" для конкретного тикера
        sql_tickers = f"""
            SELECT id, symbol, (ticker_name_map->>'YAHOO') AS yahoo_symbol
            FROM public.tickers
            WHERE id = {int(single_ticker_id)};
        """
    else:
        logging.info("📡 [УМНЫЙ ТРИГГЕР]: Формирую двухконтурную ночную выборку Universe...")
        
        # Двухконтурный запрос через UNION:
        # Контур 1: Сверхприоритетные постотчетные компании (у кого релиз был 2 дня назад) — БЕЗ ЛИМИТОВ
        # Контур 2: Фоновый плановый щит (у кого кэш протух > 90 дней или пуст) — СТРОГО LIMIT 20
        sql_tickers = """
            (
                -- КОНТУР 1: ПОСТОТЧЕТНЫЕ ЛИДЕРЫ
                SELECT id, symbol, (ticker_name_map->>'YAHOO') AS yahoo_symbol, 1 AS priority
                FROM public.tickers
                WHERE signal_next_report_date = CURRENT_DATE - INTERVAL '2 days'
                  AND (
                      provenance::text LIKE '%"MS_%' 
                      OR provenance::text LIKE '%TG_USR_ID=%'
                      OR provenance::text LIKE '%LST_ID=%'
                  )
            )
            UNION ALL
            (
                -- КОНТУР 2: РАЗМАЗЫВАЮЩИЙ ПЛАНОВЫЙ ЩИТ (Дросселирование 20 штук в сутки)
                SELECT id, symbol, (ticker_name_map->>'YAHOO') AS yahoo_symbol, 2 AS priority
                FROM public.tickers
                WHERE (fundamentals_last_synced_at IS NULL OR fundamentals_last_synced_at < CURRENT_DATE - INTERVAL '90 days')
                  AND (
                      provenance::text LIKE '%"MS_%' 
                      OR provenance::text LIKE '%TG_USR_ID=%'
                      OR provenance::text LIKE '%LST_ID=%'
                  )
                ORDER BY fundamentals_last_synced_at ASC NULLS FIRST
                LIMIT 20 -- Наша главная защита от сентябрьского затора и банов Yahoo!
            )
            ORDER BY priority ASC, id ASC;
        """

    tickers_data = db_instance.execute_query(sql_tickers)

    if not tickers_data or not isinstance(tickers_data, list):
        logging.info("ℹ️ [Yahoo Fundamentals]: На сегодняшнюю ночь плановых или постотчетных бумаг не обнаружено. Отдыхаем.")
        return {"processed": 0, "errors": 0}

    logging.info(f"📊 [Yahoo Fundamentals]: В работу взято {len(tickers_data)} бумаг(и). Инициализация парсера...")

    # Честная статистика выполнения (см. Claude/BACKLOG.md, п.25, 2026-08-03) -- раньше
    # цикл логировал "успешно завершено" безусловно, даже если часть тикеров внутри
    # батча тихо упала (try/except на уровне отдельного тикера, чтобы одна плохая бумага
    # не валила весь пакет). processed = сколько бумаг вообще пытались обновить,
    # errors = сколько из них НЕ обновились ни по какой причине (исключение, отсутствие
    # YAHOO-кода, пустой info-профиль от Yahoo).
    processed_count = 0
    error_count = 0

    for row in tickers_data:
        t_id = row['id']
        pure_symbol = row['symbol']
        processed_count += 1

        # 🔥 ИСПРАВЛЕНО: Вместо fb_ticker и локальных замен точек берем зрячий yahoo_symbol из JSONB-карты
        yf_symbol = row.get('yahoo_symbol') or pure_symbol

        if not yf_symbol or str(yf_symbol).strip().upper() == "UNSUPPORTED":
            logging.warning(f"   ⚠️ Пропуск {pure_symbol}: В ticker_name_map отсутствует валидный YAHOO-код.")
            error_count += 1
            continue
            
        try:
            logging.info(f"   • Сбор фундаментала для {pure_symbol} (Yahoo: {yf_symbol})...")
            
            ticker_obj = yf.Ticker(yf_symbol)
            info = ticker_obj.info
            
            if not info or not isinstance(info, dict):
                logging.warning(f"   ⚠️ Yahoo не вернул info-профиль для {yf_symbol}")
                error_count += 1
                continue

            # Безопасный перевод сущностей в SQL-формат
            def get_sql_val(key, multiply_100=False):
                val = info.get(key)
                if val is None:
                    return "NULL"
                if multiply_100:
                    val = float(val) * 100
                return float(val)

            pe_trailing = get_sql_val('trailingPE')
            pe_forward = get_sql_val('forwardPE')
            peg_ratio = get_sql_val('pegRatio')
            price_to_sales = get_sql_val('priceToSalesTrailing12Months')
            price_to_book = get_sql_val('priceToBook')
            ev_to_ebitda = get_sql_val('enterpriseToEbitda')

            # 🎯 ТЕКСТОВЫЙ ПАСПОРТ: Извлекаем полное имя компании из longName
            raw_comp_name = info.get('longName') or info.get('shortName') or pure_symbol
            comp_name_val = str(raw_comp_name).strip().replace("'", "''")

            sector_val = str(info.get('sector', 'Unknown Sector')).replace("'", "''")
            industry_val = str(info.get('industry', 'Unknown Industry')).replace("'", "''")

            debt_to_equity = get_sql_val('debtToEquity')
            current_ratio = get_sql_val('currentRatio')
            quick_ratio = get_sql_val('quickRatio')

            profit_margin = get_sql_val('profitMargins')
            operating_margin = get_sql_val('operatingMargins')
            return_on_equity = get_sql_val('returnOnEquity')
            return_on_assets = get_sql_val('returnOnAssets')

            dividend_yield = get_sql_val('dividendYield')
            payout_ratio = get_sql_val('payoutRatio')
            free_cash_flow = get_sql_val('freeCashflow')

            target_mean_price = get_sql_val('targetMeanPrice')
            recommendation_mean = get_sql_val('recommendationMean')
            revenue_growth_sql = get_sql_val('revenueGrowth')
            earnings_growth_sql = get_sql_val('earningsGrowth')
            target_low_price = get_sql_val('targetLowPrice')
            target_high_price = get_sql_val('targetHighPrice')

            # 🔥 МАТЕМАТИЧЕСКИЙ РАСЧЕТ CAGR ВЫРУЧКИ ЗА 3 ГОДА
            revenue_cagr_3y = "NULL"
            try:
                financials = ticker_obj.financials
                if financials is not None and 'Total Revenue' in financials.index:
                    rev_series = financials.loc['Total Revenue'].dropna()
                    if len(rev_series) >= 4:
                        revenue_current = float(rev_series.iloc[0])
                        revenue_3y_ago = float(rev_series.iloc[3])
                        if revenue_current > 0 and revenue_3y_ago > 0:
                            cagr_val = ((revenue_current / revenue_3y_ago) ** (1 / 3) - 1) * 100
                            revenue_cagr_3y = str(float(cagr_val))
            except Exception as cagr_err:
                logging.warning(f"   ⚠️ Не удалось рассчитать CAGR выручки для {yf_symbol}: {cagr_err}")

            val_exp = get_sql_val('expenseRatio')
            if val_exp != "NULL":
                expense_ratio_sql = str(float(val_exp))
            else:
                val_net_exp = get_sql_val('netExpenseRatio')
                if val_net_exp != "NULL":
                    expense_ratio_sql = str(float(val_net_exp) / 100)
                else:
                    expense_ratio_sql = "NULL"
                                
            raw_summary = info.get('longBusinessSummary')
            summary_val = str(raw_summary).strip().replace("'", "''") if raw_summary else "No summary available"

            # 🔥 ТОЧЕЧНЫЙ UPDATE-ЗАПРОС С ФИКСАЦИЕЙ ТАЙМСТЕМПА КЭША В СУБД
            sql_update = f"""
                UPDATE public.tickers 
                SET
                    company_name = '{comp_name_val}', 
                    pe_trailing = {pe_trailing}, pe_forward = {pe_forward}, peg_ratio = {peg_ratio},
                    price_to_sales = {price_to_sales}, price_to_book = {price_to_book}, ev_to_ebitda = {ev_to_ebitda},
                    debt_to_equity = {debt_to_equity}, current_ratio = {current_ratio}, quick_ratio = {quick_ratio},
                    profit_margin = {profit_margin}, operating_margin = {operating_margin}, 
                    return_on_equity = {return_on_equity}, return_on_assets = {return_on_assets},
                    dividend_yield = {dividend_yield}, payout_ratio = {payout_ratio}, free_cash_flow = {free_cash_flow},
                    target_mean_price = {target_mean_price}, recommendation_mean = {recommendation_mean},
                    target_low_price = {target_low_price}, target_high_price = {target_high_price},
                    revenue_cagr_3y = {revenue_cagr_3y}, expense_ratio = {expense_ratio_sql},
                    sector = '{sector_val}', industry = '{industry_val}',
                    long_business_summary = '{summary_val}',
                    revenue_growth = {revenue_growth_sql}, earnings_growth = {earnings_growth_sql},
                    last_updated_at = CURRENT_TIMESTAMP,
                    fundamentals_last_synced_at = CURRENT_TIMESTAMP
                WHERE id = {t_id};
            """
            db_instance.execute_query(sql_update)
            
            # Вежливая защитная пауза для Rate Limit
            time.sleep(1.5)
            
        except Exception as e:
            error_count += 1
            logging.error(f"❌ Ошибка сбора фундаментальных данных для {pure_symbol}: {e}")

    if error_count > 0:
        logging.warning(f"🏁 [Yahoo Fundamentals]: Цикл завершён С ОШИБКАМИ: {error_count} из {processed_count} бумаг не обновились.")
    else:
        logging.info(f"🏁 [Yahoo Fundamentals]: Ночной цикл анализа рынка успешно завершен ({processed_count} из {processed_count}).")

    return {"processed": processed_count, "errors": error_count}

if __name__ == "__main__":
    # Локальный запуск из консоли -- при импорте в живой процесс (cron_scheduler) уровень/формат
    # задаёт main.py централизованно (Claude/BACKLOG.md №28), этот вызов там уже не сработает
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    from database import db_sys
    sync_fundamentals(db_sys)
