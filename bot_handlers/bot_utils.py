import logging
import asyncio
from datetime import datetime, timezone

from database import db_bot, db_sys

async def execute_virtual_transfer(portfolio_id: int, listing_id: int, source_strategy_id: int, target_strategy_id: int, quantity: float):
    """
    Универсальное математическое ядро переноса долей «Источник -> Приемник».
    """
    # datetime передаём строкой -- HTTP-шлюз сериализует params в JSON, объект datetime
    # в нём не умещается (см. Claude/BACKLOG.md №81).
    system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")

    sql_asset = "SELECT id FROM public.assets WHERE portfolio_id = %s AND listing_id = %s;"
    asset_res = await asyncio.to_thread(db_bot.execute_query, sql_asset, (portfolio_id, listing_id))
    if not asset_res or len(asset_res) == 0:
        logging.error(f"🚨 [TRANSFER ERROR]: Не найден базовый актив в assets для листинга {listing_id}")
        return

    asset_row = asset_res[0] if isinstance(asset_res, list) else asset_res
    asset_id = int(asset_row['id'])

    sql_check_target = "SELECT id FROM public.strategy_assets WHERE asset_id = %s AND strategy_id = %s;"
    target_res = await asyncio.to_thread(db_bot.execute_query, sql_check_target, (asset_id, target_strategy_id))

    if not target_res or len(target_res) == 0:
        sql_insert_target = """
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            VALUES (%s, %s, 0.00, %s);
        """
        await asyncio.to_thread(db_sys.execute_query, sql_insert_target, (asset_id, target_strategy_id, system_now))

    # Честная атомарная транзакция (обе строки должны обновиться вместе или не обновиться
    # вовсе) -- execute_transaction, не ручные BEGIN/COMMIT одним текстом запроса (тот приём
    # держался только на отсутствии параметров -- простой протокол psycopg2 допускает
    # несколько операторов в одном execute(), расширенный протокол (с параметрами) -- нет,
    # см. Claude/BACKLOG.md №81).
    sql_decrement = """
        UPDATE public.strategy_assets
        SET allocated_quantity = allocated_quantity - %s, last_updated_at = %s
        WHERE asset_id = %s AND strategy_id = %s;
    """
    sql_increment = """
        UPDATE public.strategy_assets
        SET allocated_quantity = allocated_quantity + %s, last_updated_at = %s
        WHERE asset_id = %s AND strategy_id = %s;
    """
    await asyncio.to_thread(db_sys.execute_transaction, [
        (sql_decrement, (quantity, system_now, asset_id, source_strategy_id)),
        (sql_increment, (quantity, system_now, asset_id, target_strategy_id)),
    ])
    logging.info(f"✨ [UPort СУБД]: Успешный трансфер {quantity} акций из стратегии {source_strategy_id} в {target_strategy_id}")

