#!/usr/bin/env python3
import yfinance as yf
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def test_single_ticker_calendar_v2(symbol: str):
    print("\n" + "="*80)
    logging.info(f"🔍 [TEST]: Запрашиваю календарь корпоративных событий для: '{symbol}'")
    print("="*80)
    
    final_date_str = None
    
    try:
        ticker = yf.Ticker(symbol)
        raw_calendar = ticker.calendar
        
        if not raw_calendar:
            logging.warning(f"   [RESULT]: Yahoo Finance вернул пустой календарь для '{symbol}' (ETF/Европа).")
            return None
            
        if isinstance(raw_calendar, dict) and 'Earnings Date' in raw_calendar:
            earnings_dates = raw_calendar['Earnings Date']
            
            if earnings_dates and isinstance(earnings_dates, list) and len(earnings_dates) > 0:
                # Извлекаем самый первый (ближайший) объект даты из списка
                first_date_obj = earnings_dates[0]
                
                # ИНТЕЛЛЕКТУАЛЬНОЕ ПРИВЕДЕНИЕ К ФОРМАТУ СУБД TYPE date (YYYY-MM-DD)
                if hasattr(first_date_obj, 'strftime'):
                    final_date_str = first_date_obj.strftime('%Y-%m-%d')
                else:
                    # Резервный сплит, если прилетела сырая строка
                    final_date_str = str(first_date_obj).split()[0]
                    
                logging.info(f"   ✅ [УСПЕХ ДЛЯ {symbol}]: Сформирована дата для базы: '{final_date_str}'")
            else:
                logging.warning(f"   [ALERT]: Ключ 'Earnings Date' для {symbol} пуст.")
        else:
            logging.warning(f"   [ALERT]: В структуре календаря {symbol} отсутствует нужный ключ.")
            
    except Exception as e:
        logging.error(f"   🚨 [СБОЙ ТЕСТЕРА]: {e}")
        
    return final_date_str

if __name__ == "__main__":
    # Прогоняем наш калиброванный список заново
    res_aapl = test_single_ticker_calendar_v2("AAPL")
    res_brk  = test_single_ticker_calendar_v2("BRK-B")
    res_vusa = test_single_ticker_calendar_v2("VUSA.L")
    
    print("\n" + "="*80)
    print("🏁 ИТОГОВЫЙ КОНТРОЛЬНЫЙ СРЕЗ ТЕСТЕРА:")
    print(f"   🔹 AAPL  -> {res_aapl}")
    print(f"   🔹 BRK-B -> {res_brk}")
    print(f"   🔹 VUSA  -> {res_vusa}")
    print("="*80 + "\n")
