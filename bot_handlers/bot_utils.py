import logging
import asyncio
from datetime import datetime, timezone
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db_bot, db_sys
from bot_handlers.common import MenuAction

# async def format_premium_header(ticker_id: int, portfolio_id: int) -> str:
#     """
#     Универсальный сборщик укороченной премиальной шапки UPort.
#     Берет точную котировку last_price и валюту строго из таблицы listings брокера.
#     """
#     try:
#         sql_t = f"""
#             SELECT t.symbol, t.company_name, t.asset_type, t.sector, t.industry, e.exchange_code
#             FROM public.tickers t
#             LEFT JOIN public.exchanges e ON t.exchange_mic = e.mic
#             WHERE t.id = {int(ticker_id)} LIMIT 1;
#         """
#         t_res = await asyncio.to_thread(db_bot.execute_query, sql_t)
#         t = t_res[0] if isinstance(t_res, list) and len(t_res) > 0 else (t_res if isinstance(t_res, dict) else {})
        
#         if not t:
#             return "🔬 **Инструмент UPort**\n❌ Паспорт бумаги не найден в СУБД."

#         sql_l = f"SELECT last_price, currency_id FROM public.listings WHERE ticker_id = {int(ticker_id)} LIMIT 1;"
#         l_res = await asyncio.to_thread(db_bot.execute_query, sql_l)
#         l_row = l_res[0] if isinstance(l_res, list) and len(l_res) > 0 else (l_res if isinstance(l_res, dict) else {})
        
#         raw_price = float(l_row.get("last_price") or 0.0)
#         curr_id = l_row.get("currency_id", "USD")

#         sql_c = f"SELECT sign, multiplier FROM public.currencies WHERE id = '{curr_id}' LIMIT 1;"
#         c_res = await asyncio.to_thread(db_bot.execute_query, sql_c)
#         c_row = c_res[0] if isinstance(c_res, list) and len(c_res) > 0 else (c_res if isinstance(c_res, dict) else {})
        
#         sign = c_row.get("sign", f"{curr_id} ")
#         multiplier = float(c_row.get("multiplier") or 1.0)
#         live_price = raw_price * multiplier

#         display_symbol = t.get("symbol", "N/A")
#         asset_type = str(t.get('asset_type', 'EQUITY')).upper().strip()
#         type_badge = "БИРЖЕВОЙ ФОНД (ETF)" if asset_type == 'ETF' else "АКЦИЯ"
#         exch_code = t.get("exchange_code") or "GLOBAL"

#         header = (
#             f"🔬 **{display_symbol}**\n"
#             f"🏢 {t.get('company_name', 'Unknown')[:35]}\n"
#             f"📦 {t.get('sector', 'N/A')} — {t.get('industry', 'N/A')}\n"
#             f"🏷️ {type_badge}, {exch_code}\n"
#             f"───────\n"
#             f"💵 Цена рынка: **{sign}{live_price:,.2f}**\n"
#         )
#         return header
#     except Exception as e:
#         logging.error(f"🚨 [BOT UTILS ERROR]: Сбой сборщика шапки id={ticker_id}: {e}")
#         return "🔬 **Инструмент UPort**\n⚠️ Ошибка сбора паспортных данных."

# async def get_strategy_keyboard(portfolio_id: int, ticker_id: int, source_strategy_id: int, quantity: float) -> InlineKeyboardMarkup:
#     """
#     Динамически собирает универсальный пульт выбора стратегии-приемника.
#     🔥 ИСПРАВЛЕНО: Стратегия-источник исключается НА УРОВНЕ СУБД запроса (AND id !=)!
#     """
#     builder = InlineKeyboardBuilder()
    
#     # СУБД-фильтрация: запрашиваем только альтернативные стратегии
#     sql = f"""
#         SELECT id, strategy_name 
#         FROM public.strategies 
#         WHERE portfolio_id = {int(portfolio_id)} 
#           AND is_active = true 
#           AND id != {int(source_strategy_id)};
#     """
#     strategies = await asyncio.to_thread(db_bot.execute_query, sql)
    
#     if isinstance(strategies, list):
#         for strat in strategies:
#             strat_id = int(strat['id'])
            
#             # 🔥 УНИВЕРСАЛЬНАЯ СБОРКА: В sub_view прячем связку "Источник/Количество" через косую черту
#             # В task_id передаем ID стратегии-приемника (куда инвестор кликнул)
#             builder.row(types.InlineKeyboardButton(
#                 text=f"📥 {strat['strategy_name']}",
#                 callback_data=MenuAction(
#                     action="move_confirm",
#                     portfolio_id=portfolio_id,
#                     ticker_id=ticker_id,
#                     sub_view=f"{int(source_strategy_id)}/{int(quantity)}",
#                     task_id=strat_id
#                 ).pack()
#             ))
            
#     builder.row(types.InlineKeyboardButton(text="💤 Оставить в покое", callback_data=MenuAction(action="main_menu").pack()))
#     return builder.as_markup()

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

