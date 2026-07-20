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
    sql_src = f"SELECT strategy_name FROM public.strategies WHERE id = {int(source_id)} LIMIT 1;"
    sql_tgt = f"SELECT strategy_name FROM public.strategies WHERE id = {int(target_id)} LIMIT 1;"
    
    src_res = await asyncio.to_thread(db_bot.execute_query, sql_src)
    tgt_res = await asyncio.to_thread(db_bot.execute_query, sql_tgt)
    
    src_row = src_res[0] if isinstance(src_res, list) and len(src_res) > 0 else {}
    tgt_row = tgt_res[0] if isinstance(tgt_res, list) and len(tgt_res) > 0 else {}
    
    src_name = src_row.get("strategy_name", "Исходная")
    tgt_name = tgt_row.get("strategy_name", "Целевая")

    header_text = await format_premium_header(ticker_id, portfolio_id)
    
    # ИСПОЛЬЗУЕМ УНИВЕРСАЛЬНЫЙ СБОРЩИК ЭКРАНА С ХАРДКОРНЫМ ФЛАГОМ HTML
    confirm_text = generate_confirm_screen(
        header_text=header_text,
        action_title="ПОДТВЕРЖДЕНИЕ ОПЕРАЦИИ",
        details_list=[
            f"Перенести {quantity} шт. из стратегии: {src_name}",
            f"В стратегию: {tgt_name}"
        ],
        parse_mode="HTML"
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

    await callback.message.edit_text(confirm_text, parse_mode="HTML", reply_markup=reply_markup)
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
        sql_l = f"SELECT id FROM public.listings WHERE ticker_id = {int(ticker_id)} LIMIT 1;"
        l_res = await asyncio.to_thread(db_bot.execute_query, sql_l)
        l_row = l_res[0] if isinstance(l_res, list) and len(l_res) > 0 else (l_res if isinstance(l_res, dict) else {})
        listing_id = int(l_row.get("id", 0))

        sql_name = f"SELECT strategy_name FROM public.strategies WHERE id = {int(target_id)} LIMIT 1;"
        n_res = await asyncio.to_thread(db_bot.execute_query, sql_name)
        n_row = n_res[0] if isinstance(n_res, list) and len(n_res) > 0 else {}
        target_name = n_row.get("strategy_name", "Выбранную стратегию")

        # Проводим транзакцию в СУБД через db_sys
        await execute_virtual_transfer(
            portfolio_id=portfolio_id,
            listing_id=listing_id,
            source_strategy_id=source_id,
            target_strategy_id=target_id,
            quantity=float(quantity)
        )

        success_text = (
            f"✨ <b>[UPort Успех]: Транзакция зафиксирована</b>\n\n"
            f"📥 <b>Направление</b>: Разбор Нераспределенных ➔ {target_name}\n"
            f"📦 <b>Объем</b>: {quantity} шт. целых лотов\n\n"
            f"🔒 Балансы виртуальных стратегий портфеля успешно пересчитаны в СУБД через шлюз db_sys."
        )
        
        # ИСПОЛЬЗУЕМ УНИВЕРСАЛЬНУЮ КЛАВИАТУРУ В РЕЖИМЕ ОДИНОЧНОЙ КНОПКИ УСПЕХА
        reply_markup = generate_confirm_keyboard(
            yes_text="💤 Главное меню",
            yes_callback_packed=MenuAction(action="main_menu").pack(),
            no_text="",  # Передаем пустую строку, пульт сам схлопнется до одной кнопки
            no_callback_packed=""
        )
        
        await callback.message.edit_text(success_text, parse_mode="HTML", reply_markup=reply_markup)
    
    await callback.answer()
