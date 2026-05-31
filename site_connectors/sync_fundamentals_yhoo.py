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
        WHERE w.status != 'archived'::public.ticker_lifecycle_status;
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

            # 🔥 ДОКАЧКА НОВЫХ ПАРАМЕТРОВ v3.8 (ПО ВАШЕМУ СТАНДАРТУ get_sql_val)
            # 1. Числовые темпы роста — пропускаем через вашу микро-функцию
            # Числовые темпы роста — пропускаем через вашу микро-функцию
            revenue_growth_sql = get_sql_val('revenueGrowth')
            earnings_growth_sql = get_sql_val('earningsGrowth')

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
                                
            # 2. Текстовое описание бизнеса — забираем безопасно как строку
            # Экранируем одинарные кавычки ("'" на "''"), чтобы SQL-запрос не падал
            raw_summary = info.get('longBusinessSummary')
            if raw_summary is not None:
                summary_val = str(raw_summary).strip().replace("'", "''")
            else:
                summary_val = "No summary available"


            # Формируем точечный UPDATE-запрос для карточки тикера
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
            
        time.sleep(1.0) # Повышенная защитная пауза для тяжелых ночных запросов info
        
    logging.info("🏁 [Yahoo Fundamentals]: Ночной цикл анализа рынка успешно завершен.")
