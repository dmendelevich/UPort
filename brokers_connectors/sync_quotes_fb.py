import os
from datetime import datetime

def sync_quotes_fb_branch(tickers_data, db_instance, fb_client_class):
    """
    Пакетный REST-обновитель цен акций Freedom Broker Казахстан.
    Принимает список тикеров, опрашивает getStockQuotesJson и обновляет last_price и company_name.
    """
    if not tickers_data:
        return

    # Динамически собираем список тикеров для отправки одной пачкой
    tickers_list = [row['full_ticker'] for row in tickers_data]
    print(f"📡 [REST FB]: Запрос котировок getStockQuotesJson для: {tickers_list}")

    # Инициализируем базовый FreedomBrokerClient (DLM Т-ключи)
    api_key = os.getenv("FB_DLM_API_KEY")
    api_secret = os.getenv("FB_DLM_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ [REST FB ERROR]: В .env отсутствуют базовые ключи FB_DLM для запроса котировок.")
        return

    fb_client = fb_client_class(public_key=api_key, private_key=api_secret)

    try:
        # Отправляем пакетный запрос строго по нашему проверенному шаблону execute
        raw_res = fb_client.execute(command="getStockQuotesJson", params={"tickers": tickers_list})
        
        if isinstance(raw_res, dict) and "error" in raw_res:
            print(f"❌ [REST FB API ERROR]: {raw_res['error']}")
            return
            
        # СТРОГО ПО ТЕСТУ: проваливаемся в result -> q
        result_node = raw_res.get("result", {}) if isinstance(raw_res, dict) else {}
        result_array = result_node.get("q", []) if isinstance(result_node, dict) else []
        
        if not result_array or not isinstance(result_array, list):
            print("⚠️ [REST FB]: Сервер брокера вернул пустой массив котировок.")
            return

        # Мапим множители и внутренние ID из данных СУБД
        multipliers_map = {row['full_ticker']: float(row['multiplier']) for row in tickers_data}
        id_map = {row['full_ticker']: int(row['id']) for row in tickers_data}

        for quote in result_array:
            # СТРОГО ПО ТЕСТУ: тикер лежит в ключе 'c', а цена в 'ltp'
            ticker = quote.get("c")
            if not ticker or ticker not in multipliers_map:
                continue
                
            raw_price = quote.get("ltp")
            comp_name = quote.get("name", "")
            
            # Экранируем одинарные кавычки в названии компании (напр. Apple Inc.), чтобы SQL-запрос не падал
            if comp_name:
                comp_name = comp_name.replace("'", "''")

            if raw_price is not None and str(raw_price) != 'nan':
                # Умножаем на multiplier (напр. 0.01 для пенсов)
                final_price = float(raw_price) * multipliers_map[ticker]
                t_id = id_map[ticker]
                
                # Обновляем last_price, company_name и таймстамп
                sql_update = f"""
                    UPDATE public.tickers 
                    SET last_price = {final_price}, 
                        company_name = '{comp_name}', 
                        last_updated_at = '{datetime.now()}' 
                    WHERE id = {t_id};
                """
                db_instance.execute_query(sql_update)
                print(f"   • {ticker} успешно актуализирован: {final_price:.2f}")
            else:
                print(f"   • {ticker} ⚠️ В пакете отсутствует значение ltp.")
                
    except Exception as e:
        print(f"❌ [REST FB CRITICAL ERROR]: Сбой пакетного апдейта котировок: {e}")
