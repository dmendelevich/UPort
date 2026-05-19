def sync_quotes_t212_branch(tickers_data, db_instance):
    """
    REST-обновитель цен акций Trading 212 (UK) для портфеля сына (MDM).
    Временно работает в режиме заглушки до настройки интеграции с их API v0.
    """
    if not tickers_data:
        return
        
    tickers_list = [row['full_ticker'] for row in tickers_data]
    print(f"⏱️ [REST T212]: Контур Trading 212 зафиксировал тикеры сына: {tickers_list}")
    print("ℹ️ [REST T212]: Ветка находится в режиме ожидания реализации API v0.")
