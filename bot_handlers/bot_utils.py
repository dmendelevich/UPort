import logging
import asyncio
from datetime import datetime, timezone

from database import db_bot, db_sys

async def execute_virtual_transfer(portfolio_id: int, listing_id: int, source_strategy_id: int, target_strategy_id: int, quantity: float):
    """
    Универсальное математическое ядро переноса долей «Источник -> Приемник».
    """
    system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    
    sql_asset = f"SELECT id FROM public.assets WHERE portfolio_id = {int(portfolio_id)} AND listing_id = {int(listing_id)};"
    asset_res = await asyncio.to_thread(db_bot.execute_query, sql_asset)
    if not asset_res or len(asset_res) == 0:
        logging.error(f"🚨 [TRANSFER ERROR]: Не найден базовый актив в assets для листинга {listing_id}")
        return
        
    asset_row = asset_res[0] if isinstance(asset_res, list) else asset_res
    asset_id = int(asset_row['id'])

    sql_check_target = f"SELECT id FROM public.strategy_assets WHERE asset_id = {asset_id} AND strategy_id = {int(target_strategy_id)};"
    target_res = await asyncio.to_thread(db_bot.execute_query, sql_check_target)
    
    if not target_res or len(target_res) == 0:
        sql_insert_target = f"""
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            VALUES ({asset_id}, {int(target_strategy_id)}, 0.00, '{system_now}');
        """
        await asyncio.to_thread(db_sys.execute_query, sql_insert_target)

    sql_move = f"""
        BEGIN;
        UPDATE public.strategy_assets 
        SET allocated_quantity = allocated_quantity - {float(quantity)}, last_updated_at = '{system_now}'
        WHERE asset_id = {asset_id} AND strategy_id = {int(source_strategy_id)};
        
        UPDATE public.strategy_assets 
        SET allocated_quantity = allocated_quantity + {float(quantity)}, last_updated_at = '{system_now}'
        WHERE asset_id = {asset_id} AND strategy_id = {int(target_strategy_id)};
        COMMIT;
    """
    await asyncio.to_thread(db_sys.execute_query, sql_move)
    logging.info(f"✨ [UPort СУБД]: Успешный трансфер {quantity} акций из стратегии {source_strategy_id} в {target_strategy_id}")

