import logging

def sync_quotes_t212_branch(tickers_data, db_instance):
    """
    REST-обновитель цен акций Trading 212 (UK) для портфеля сына (MDM).
    Временно работает в режиме заглушки до настройки интеграции с их API v0.
    """
    if not tickers_data:
        return

    tickers_list = [row['full_ticker'] for row in tickers_data]
    logging.debug(f"⏱️ [REST T212]: Контур Trading 212 зафиксировал {len(tickers_list)} тикеров сына: {tickers_list}")
    logging.debug("ℹ️ [REST T212]: Ветка находится в режиме ожидания реализации API v0.")
