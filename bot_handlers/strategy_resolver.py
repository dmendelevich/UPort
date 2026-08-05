import logging
import asyncio
from aiogram import Router, types, F

from database import db_sys, db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_utils import execute_virtual_transfer
from bot_handlers.bot_screens import format_premium_header, generate_confirm_screen
from bot_handlers.bot_keyboards import generate_confirm_keyboard

router = Router()

@router.callback_query(MenuAction.filter(F.action == "move_confirm"))
async def process_universal_confirm_screen(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    ЭТАП 1: Вывод универсального экрана подтверждения действия.
    🔥 РЕФАКТОРИНГ: Подключение универсального экрана и универсальной клавиатуры.
    """
    portfolio_id = callback_data.portfolio_id
    ticker_id = callback_data.ticker_id
    target_id = callback_data.task_id 
    
    # Распаковываем связку "Источник/Количество" из входящего sub_view кнопки
    payload = callback_data.sub_view.split("/")
    source_id = int(payload[0])
    quantity = int(payload[1])
    
    # Запрашиваем из базы человеческие имена обеих стратегий для зрячего вопроса
    sql_src = "SELECT strategy_name FROM public.strategies WHERE id = %s LIMIT 1;"
    sql_tgt = "SELECT strategy_name FROM public.strategies WHERE id = %s LIMIT 1;"

    src_res = await asyncio.to_thread(db_bot.execute_query, sql_src, (source_id,))
    tgt_res = await asyncio.to_thread(db_bot.execute_query, sql_tgt, (target_id,))
    
    src_row = src_res[0] if isinstance(src_res, list) and len(src_res) > 0 else {}
    tgt_row = tgt_res[0] if isinstance(tgt_res, list) and len(tgt_res) > 0 else {}
    
    src_name = src_row.get("strategy_name", "Исходная")
    tgt_name = tgt_row.get("strategy_name", "Целевая")

    header_text = await format_premium_header(ticker_id, portfolio_id)
    
    # Единый Markdown-стандарт всего UPort (см. Claude/05_strategy_screen_and_kubiki.md) --
    # HTML здесь раньше был обходом путаницы MarkdownV2/Markdown, не проблемы Markdown как такового.
    confirm_text = generate_confirm_screen(
        header_text=header_text,
        action_title="ПОДТВЕРЖДЕНИЕ ОПЕРАЦИИ",
        details_list=[
            f"Перенести {quantity} шт. из стратегии: {src_name}",
            f"В стратегию: {tgt_name}"
        ],
        parse_mode="Markdown"
    )

    # ИСПОЛЬЗУЕМ УНИВЕРСАЛЬНЫЙ ПУЛЬТ ПОДТВЕРЖДЕНИЯ ДА/НЕТ
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, подтверждаю перенос",
        yes_callback_packed=MenuAction(
            action="move_execute",
            portfolio_id=portfolio_id,
            ticker_id=ticker_id,
            sub_view=f"transfer/{source_id}/{quantity}",
            task_id=target_id  
        ).pack(),
        no_text="❌ Отменить операцию",
        no_callback_packed=MenuAction(action="main_menu").pack()
    )

    await callback.message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)
    await callback.answer()


@router.callback_query(MenuAction.filter(F.action == "move_execute"))
async def process_universal_execute(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    ЭТАП 2: Проведение транзакции в СУБД и вывод финального отчета.
    """
    portfolio_id = callback_data.portfolio_id
    ticker_id = callback_data.ticker_id
    
    payload = callback_data.sub_view.split("/")
    action_type = payload[0]
    source_id = int(payload[1])
    quantity = int(payload[2])
    
    target_id = callback_data.task_id 

    if action_type == "transfer":
        sql_l = "SELECT l.id, t.symbol FROM public.listings l JOIN public.tickers t ON t.id = l.ticker_id WHERE l.ticker_id = %s LIMIT 1;"
        l_res = await asyncio.to_thread(db_bot.execute_query, sql_l, (ticker_id,))
        l_row = l_res[0] if isinstance(l_res, list) and len(l_res) > 0 else (l_res if isinstance(l_res, dict) else {})
        listing_id = int(l_row.get("id", 0))
        symbol = l_row.get("symbol", "бумага")

        sql_names = "SELECT id, strategy_name FROM public.strategies WHERE id IN (%s, %s);"
        names_res = await asyncio.to_thread(db_bot.execute_query, sql_names, (source_id, target_id))
        names_res = names_res if isinstance(names_res, list) else ([names_res] if names_res else [])
        names_by_id = {int(r["id"]): r["strategy_name"] for r in names_res}
        source_name = names_by_id.get(source_id, "исходной стратегии")
        target_name = names_by_id.get(target_id, "выбранной стратегии")

        await execute_virtual_transfer(
            portfolio_id=portfolio_id,
            listing_id=listing_id,
            source_strategy_id=source_id,
            target_strategy_id=target_id,
            quantity=float(quantity)
        )

        success_text = (
            f"✅ Перенос выполнен.\n\n"
            f"{abs(quantity)} шт. {symbol} переведено из «{source_name}» в «{target_name}»."
        )
        
        # ИСПОЛЬЗУЕМ УНИВЕРСАЛЬНУЮ КЛАВИАТУРУ В РЕЖИМЕ ОДИНОЧНОЙ КНОПКИ УСПЕХА
        reply_markup = generate_confirm_keyboard(
            yes_text="💤 Главное меню",
            yes_callback_packed=MenuAction(action="main_menu").pack(),
            no_text="",  # Передаем пустую строку, пульт сам схлопнется до одной кнопки
            no_callback_packed=""
        )
        
        await callback.message.edit_text(success_text, parse_mode="Markdown", reply_markup=reply_markup)
    
    await callback.answer()
