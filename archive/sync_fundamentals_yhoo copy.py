import yfinance as yf
from datetime import datetime
import time
import logging

def sync_fundamentals(db_instance):
    """
    Контур В: Ночное обновление 18 фундаментальных показателей компаний через Yahoo Finance.
    Берет тикеры со статусом 'active' или 'watchlist'. Идеально решает проблему BRK.B через поле symbol.
    """
    logging.info("📡 [Yahoo Fundamentals]: Выборка отслеживаемых тикеров из базы данных...")
    
    # 🔥 АКАДЕМИЧЕСКИЙ ЗАПРОС v3.0: Собираем уникальные глобальные тикеры, которые не заархивированы семьей
    sql_tickers = """
        SELECT DISTINCT t.id, t.symbol, l.broker_symbol AS full_ticker
        FROM public.watchlist w
        JOIN public.listings l ON w.listing_id = l.id
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE (w.considered_at IS NOT NULL OR w.watched_at IS NOT NULL OR w.ordered_at IS NOT NULL OR w.bought_at IS NOT NULL);
    """
    tickers_data = db_instance.execute_query(sql_tickers)

    
    if not tickers_data or not isinstance(tickers_data, list):
        logging.info("ℹ️ [Yahoo Fundamentals]: Нет тикеров со статусом active или watchlist для обновления.")
        return

    logging.info(f"📊 [Yahoo Fundamentals]: Запуск ночного анализа для {len(tickers_data)} бумаг.")

    for row in tickers_data:
        t_id = row['id']
        pure_symbol = row['symbol']
        full_ticker = row['full_ticker']
        
        try:
            # РЕШЕНИЕ ПРОБЛЕМЫ BRK.B: Yahoo ждет дефис вместо точки в именах классов акций
            yf_symbol = pure_symbol.replace('.', '-') if pure_symbol else pure_symbol
            if not yf_symbol:
                continue

            logging.info(f"   • Сбор фундаментала для {full_ticker} (Yahoo: {yf_symbol})...")
            
            # Делаем тяжелый запрос полного профиля компании
            ticker_obj = yf.Ticker(yf_symbol)
            info = ticker_obj.info
            
            if not info or not isinstance(info, dict):
                logging.warning(f"   ⚠️ Yahoo не вернул info-профиль для {yf_symbol}")
                continue

            # Вспомогательная микро-функция для безопасного перевода сущностей в SQL-формат
            def get_sql_val(key, multiply_100=False):
                val = info.get(key)
                if val is None:
                    return "NULL"
                if multiply_100: # Для маржинальности и дивдоходности, если Yahoo отдает их как 0.05 вместо 5%
                    val = float(val) * 100
                return float(val)

            # Безопасно собираем все 18 параметров через .get()
            pe_trailing = get_sql_val('trailingPE')
            pe_forward = get_sql_val('forwardPE')
            peg_ratio = get_sql_val('pegRatio')
            price_to_sales = get_sql_val('priceToSalesTrailing12Months')
            price_to_book = get_sql_val('priceToBook')
            ev_to_ebitda = get_sql_val('enterpriseToEbitda')

            # 🔥 ДОКАЧКА ТЕКСТОВОГО ПАСПОРТА КОМПАНИИ
            sector_val = info.get('sector', 'Unknown Sector').replace("'", "''")
            industry_val = info.get('industry', 'Unknown Industry').replace("'", "''")

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

            # 🔥 ДОКАЧКА НОВЫХ ПАРАМЕТРОВ v4.0 (ПОДТВЕРЖДЕНО РАЗВЕДКОЙ БОЕМ)
            revenue_growth_sql = get_sql_val('revenueGrowth')
            earnings_growth_sql = get_sql_val('earningsGrowth')

            # 1. ЦЕЛЕВЫЕ ДИАПАЗОНЫ АНАЛИТИКОВ ДЛЯ СВИНГ-ТРЕЙДИНГА П10 (Ключи из info)
            target_low_price = get_sql_val('targetLowPrice')
            target_high_price = get_sql_val('targetHighPrice')

            # 2. МАТЕМАТИЧЕСКИЙ РАСЧЕТ 14-ДНЕВНОГО RSI (Из полученных 20 торговых сессий)
            rsi_14 = "NULL"
            try:
                hist = ticker_obj.history(period="1mo")
                if hist is not None and len(hist) >= 15:
                    delta = hist['Close'].diff()
                    gain = delta.clip(lower=0)
                    loss = -delta.clip(upper=0)
                    # Вычисляем экспоненциальные скользящие средние по стандарту Уайлдера (RSI)
                    avg_gain = gain.ewm(com=13, adjust=False).mean()
                    avg_loss = loss.ewm(com=13, adjust=False).mean()
                    rs = avg_gain / avg_loss.replace(0, 0.00001) # Защита от деления на ноль
                    rsi_val = 100 - (100 / (1 + rs.iloc[-1]))
                    if 0 <= rsi_val <= 100:
                        rsi_14 = str(float(rsi_val))
            except Exception as rsi_err:
                logging.warning(f"   ⚠️ Не удалось математически рассчитать RSI для {yf_symbol}: {rsi_err}")

            # 3. МАТЕМАТИЧЕСКИЙ РАСЧЕТ CAGR ВЫРУЧКИ ЗА 3 ГОДА ДЛЯ СТРАТЕГИИ П136 (Индексы 0 и 3)
            revenue_cagr_3y = "NULL"
            try:
                financials = ticker_obj.financials
                if financials is not None and 'Total Revenue' in financials.index:
                    rev_series = financials.loc['Total Revenue'].dropna()
                    if len(rev_series) >= 4:
                        # Индекс 0 — свежий год (2025), Индекс 3 — три года назад (2022)
                        revenue_current = float(rev_series.iloc[0])
                        revenue_3y_ago = float(rev_series.iloc[3])
                        if revenue_current > 0 and revenue_3y_ago > 0:
                            cagr_val = ((revenue_current / revenue_3y_ago) ** (1 / 3) - 1) * 100
                            revenue_cagr_3y = str(float(cagr_val))
            except Exception as cagr_err:
                logging.warning(f"   ⚠️ Не удалось рассчитать CAGR выручки для {yf_symbol}: {cagr_err}")

            # Математический каскад со строковым форматированием для пробития ограничений БД
            val_exp = get_sql_val('expenseRatio')
            if val_exp != "NULL":
                expense_ratio_sql = str(float(val_exp))
            else:
                val_net_exp = get_sql_val('netExpenseRatio')
                if val_net_exp != "NULL":
                    expense_ratio_sql = str(float(val_net_exp) / 100)
                else:
                    expense_ratio_sql = "NULL"
                                
            # Текстовое описание бизнеса — забираем безопасно как строку
            raw_summary = info.get('longBusinessSummary')
            if raw_summary is not None:
                summary_val = str(raw_summary).strip().replace("'", "''")
            else:
                summary_val = "No summary available"

            # 🔥 МОДЕРНИЗИРОВАННЫЙ ТОЧЕЧНЫЙ UPDATE-ЗАПРОС UPORT v4.0 (СТРОГО ПО НАШЕЙ ТАБЛИЦЕ TICKERS)
            sql_update = f"""
                UPDATE public.tickers 
                SET 
                    pe_trailing = {pe_trailing}, pe_forward = {pe_forward}, peg_ratio = {peg_ratio},
                    price_to_sales = {price_to_sales}, price_to_book = {price_to_book}, ev_to_ebitda = {ev_to_ebitda},
                    debt_to_equity = {debt_to_equity}, current_ratio = {current_ratio}, quick_ratio = {quick_ratio},
                    profit_margin = {profit_margin}, operating_margin = {operating_margin}, 
                    return_on_equity = {return_on_equity}, return_on_assets = {return_on_assets},
                    dividend_yield = {dividend_yield}, payout_ratio = {payout_ratio}, free_cash_flow = {free_cash_flow},
                    target_mean_price = {target_mean_price}, recommendation_mean = {recommendation_mean},
                    target_low_price = {target_low_price}, target_high_price = {target_high_price},
                    rsi_14 = {rsi_14}, revenue_cagr_3y = {revenue_cagr_3y},
                    sector = '{sector_val}', industry = '{industry_val}',
                    long_business_summary = '{summary_val}',
                    revenue_growth = {revenue_growth_sql},
                    earnings_growth = {earnings_growth_sql},
                    expense_ratio = {expense_ratio_sql},
                    last_updated_at = CURRENT_TIMESTAMP
                WHERE id = {t_id};
            """

            db_instance.execute_query(sql_update)
            
        except Exception as e:
            logging.error(f"❌ Ошибка сбора фундаментальных данных для {full_ticker}: {e}")
            
        time.sleep(1.0) # Защитная пауза для тяжелых ночных запросов info
        
    logging.info("🏁 [Yahoo Fundamentals]: Ночной цикл анализа рынка успешно завершен.")
