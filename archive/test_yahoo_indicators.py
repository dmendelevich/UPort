#!/usr/bin/env python3
import yfinance as yf
import logging
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def test_yahoo_all_indicators(symbol: str):
    print("\n" + "="*100)
    logging.info(f"📊 [TEST]: Глубокий теханализ и метаданные (1y) для: '{symbol}'")
    print("="*100)
    
    try:
        ticker_obj = yf.Ticker(symbol)
        
        # 🔬 РЕЗВЕДКА YAHOO FAST_INFO (Исследуем метаданные для Шага 3)
        print(f"\n   🔎 [АНАЛИЗ FAST_INFO ДЛЯ СУБД - '{symbol}']:")
        try:
            yf_exchange = ticker_obj.fast_info.get("exchange")
            yf_quote_type = ticker_obj.fast_info.get("quoteType")
            print(f"      ▪️ fast_info.get('exchange'):  【 {yf_exchange} 】")
            print(f"      ▪️ fast_info.get('quoteType'): 【 {yf_quote_type} 】")
        except Exception as fast_err:
            print(f"      🚨 Ошибка извлечения fast_info: {fast_err}")

        hist = ticker_obj.history(period="1y")
        
        if hist is None or hist.empty:
            logging.warning(f"   ❌ Yahoo Finance вернул пустой массив для '{symbol}'")
            return
            
        total_days = len(hist)
        if total_days < 200:
            logging.warning(f"   ⚠️ Недостаточно дней ({total_days}) для полноценного анализа. Пропуск.")
            return

        # 🧮 1. БАЗОВАЯ ЦЕНА И ВАШИ ПЕРВЫЕ ИНДИКАТОРЫ
        current_price = float(hist['Close'].iloc[-1])
        sma_100 = float(hist['Close'].tail(100).mean())
        sma_200 = float(hist['Close'].tail(200).mean())
        
        # 🧮 2. НОВЫЕ ИНДИКАТОРЫ: EMA-20 и SMA-50
        ema_20 = float(hist['Close'].ewm(span=20, adjust=False).mean().iloc[-1])
        sma_50 = float(hist['Close'].tail(50).mean())
        
        # 🧮 3. РАСЧЕТ RSI-14
        delta = hist['Close'].diff()
        gain = (delta.where(delta > 0, 0)).copy()
        loss = (-delta.where(delta < 0, 0)).copy()
        
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        
        rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_14 = float(rsi_series.iloc[-1])
        
        # 🧮 4. РАСЧЕТ ЛИНИИ MACD
        ema_12 = hist['Close'].ewm(span=12, adjust=False).mean()
        ema_26 = hist['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        macd_val = float(macd_line.iloc[-1])

        # 🧮 5. ВЫЧИСЛЕНИЕ РЕКОМЕНДАЦИИ
        if current_price > sma_200 and current_price > sma_100:
            recommendation = "STRONG_BUY"
        elif current_price < sma_200 and current_price < sma_100:
            recommendation = "SELL"
        else:
            recommendation = "NEUTRAL"
            
        print(f"\n   🎯 [МЕГА-СКОРИНГ ДЛЯ СУБД - '{symbol}']:")
        print(f"      ▪️ Текущая цена:       {current_price:.4f}")
        print(f"      ▪️ Линия EMA-20:       {ema_20:.4f}")
        print(f"      ▪️ Линия SMA-50:       {sma_50:.4f}")
        print(f"      ▪️ Линия SMA-100:      {sma_100:.4f}")
        print(f"      ▪️ Линия SMA-200:      {sma_200:.4f}")
        print(f"      ▪️ Осциллятор RSI-14:  {rsi_14:.2f}  " + ("🔥 ПЕРЕКУПЛЕННОСТЬ (>70)" if rsi_14 > 70 else ("🧊 ПЕРЕПРОДАННОСТЬ (<30)" if rsi_14 < 30 else "(Норма)")))
        print(f"      ▪️ Тренд MACD Line:    {macd_val:.4f}")
        print(f"      ▪️ Сигнал системы:     【 {recommendation} 】")
        
    except Exception as e:
        logging.error(f"   🚨 Сбой расчета индикаторов для {symbol}: {e}")

if __name__ == "__main__":
    # Тестируем акцию, ETF-фонд и специальный американский класс акций (через дефис)
    test_yahoo_all_indicators("AAPL")
    test_yahoo_all_indicators("VUSA.L")
    test_yahoo_all_indicators("BRK-B")
    print("\n" + "="*100 + "\n🏁 Тест завершен!")
