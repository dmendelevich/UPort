import asyncio
import logging
from aiogram import Router, types, F, Bot
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_screens import generate_confirm_screen
from bot_handlers.bot_keyboards import generate_confirm_keyboard, generate_nav_back_keyboard

router = Router()

# Содержательные стратегии, для которых вообще имеет смысл план входа (см. Claude/03_strategies_and_templates.md)
CONTENT_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING")


class NewPipelineStates(StatesGroup):
    waiting_for_target_quantity = State()


def _back_to_ticker_keyboard(portfolio_id: int, listing_id: int):
    return generate_nav_back_keyboard(
        one_step_back_text="🔙 К бумаге",
        full_back_callback=MenuAction(action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id, sub_view="owner").pack()
    )


# --- ЭКРАН 1: СПИСОК НЕПРИВЯЗАННЫХ АКТИВНЫХ ОРДЕРОВ ---

@router.callback_query(MenuAction.filter(F.action == "pipeline_link_start"))
async def process_link_start(callback: types.CallbackQuery, callback_data: MenuAction):
    """Показывает активные ордера этого тикера/портфеля, ещё не привязанные ни к одному шагу плана."""
    p_id = callback_data.portfolio_id
    t_id = callback_data.ticker_id
    l_id = callback_data.listing_id

    await callback.answer()

    sql = f"""
        SELECT o.id, o.broker_order_id, o.q, o.p, o.oper
        FROM public.orders o
        WHERE o.portfolio_id = {int(p_id)} AND o.ticker_id = {int(t_id)}
          AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED')
          AND NOT EXISTS (
              SELECT 1 FROM public.order_pipelines op
              WHERE op.pending_broker_order_id = o.broker_order_id
          )
        ORDER BY o.created_at DESC;
    """
    orders = await asyncio.to_thread(db_bot.execute_query, sql)
    orders = orders if isinstance(orders, list) else ([orders] if orders else [])

    back_kb = _back_to_ticker_keyboard(p_id, l_id)

    if not orders:
        try:
            await callback.message.edit_text(
                "🔗 Нет активных приказов по этой бумаге, ещё не привязанных к плану.\n"
                "Сначала поставьте ордер в терминале брокера.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
        return

    builder = InlineKeyboardBuilder()
    for o in orders:
        op_label = "Покупка" if int(o["oper"]) in (1, 2) else "Продажа"
        builder.row(types.InlineKeyboardButton(
            text=f"{op_label} {float(o['q']):.0f} шт по {float(o['p'] or 0):.2f} (№{o['broker_order_id']})",
            callback_data=MenuAction(
                action="pipeline_link_order", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id, order_id=int(o["id"])
            ).pack()
        ))

    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            "🔗 **Привязка ордера к плану**\n\nВыберите ордер, который относится к плану входа/выхода:",
            parse_mode="Markdown",
            reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


# --- ЭКРАН 2: ОРДЕР ВЫБРАН -- ИЩЕМ АКТИВНЫЙ ПЛАН ПО ЭТОЙ БУМАГЕ ---

@router.callback_query(MenuAction.filter(F.action == "pipeline_link_order"))
async def process_link_order(callback: types.CallbackQuery, callback_data: MenuAction):
    """Ордер выбран. Если по тикеру уже есть активный план(ы) -- предлагаем привязать к нему(ним),
    если нет ни одного -- ведём в сценарий создания нового плана (выбор стратегии)."""
    p_id = callback_data.portfolio_id
    t_id = callback_data.ticker_id
    l_id = callback_data.listing_id
    o_id = callback_data.order_id

    await callback.answer()

    sql_pipelines = f"""
        SELECT op.strategy_id, op.current_step, s.strategy_name
        FROM public.order_pipelines op
        JOIN public.strategies s ON op.strategy_id = s.id
        WHERE op.portfolio_id = {int(p_id)} AND op.ticker_id = {int(t_id)}
          AND op.pipeline_status IN ('PENDING', 'ACTIVE')
          AND op.pending_broker_order_id IS NULL;
    """
    pipelines = await asyncio.to_thread(db_bot.execute_query, sql_pipelines)
    pipelines = pipelines if isinstance(pipelines, list) else ([pipelines] if pipelines else [])

    back_kb = _back_to_ticker_keyboard(p_id, l_id)

    if not pipelines:
        # Нет ни одного плана, ждущего шаг по этой бумаге -- заводим новый (или все уже ждут другой ордер)
        sql_strategies = f"""
            SELECT s.id, s.strategy_name
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = {int(p_id)} AND s.is_active = true
              AND st.system_key IN {CONTENT_SYSTEM_KEYS}
            ORDER BY s.strategy_name;
        """
        strategies = await asyncio.to_thread(db_bot.execute_query, sql_strategies)
        strategies = strategies if isinstance(strategies, list) else ([strategies] if strategies else [])

        if not strategies:
            try:
                await callback.message.edit_text(
                    "⚠️ В этом портфеле нет ни одной активной содержательной стратегии.",
                    reply_markup=back_kb
                )
            except TelegramBadRequest:
                pass
            return

        builder = InlineKeyboardBuilder()
        for s in strategies:
            builder.row(types.InlineKeyboardButton(
                text=f"🎯 {s['strategy_name']}",
                callback_data=MenuAction(
                    action="pipeline_new_strategy", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id,
                    order_id=o_id, strategy_id=int(s["id"])
                ).pack()
            ))
        final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
        final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

        try:
            await callback.message.edit_text(
                "🆕 По этой бумаге нет плана, ожидающего этот ордер -- заводим новый.\n\nВыберите стратегию:",
                reply_markup=final_builder.as_markup()
            )
        except TelegramBadRequest:
            pass
        return

    if len(pipelines) == 1:
        pipe = pipelines[0]
        await _show_link_confirm(callback, p_id, t_id, l_id, o_id, int(pipe["strategy_id"]), pipe["strategy_name"], int(pipe["current_step"]))
        return

    # Больше одного активного плана по этой бумаге (бумага в нескольких стратегиях) -- просим уточнить
    builder = InlineKeyboardBuilder()
    for pipe in pipelines:
        builder.row(types.InlineKeyboardButton(
            text=f"🎯 {pipe['strategy_name']} (шаг {pipe['current_step']})",
            callback_data=MenuAction(
                action="pipeline_link_confirm", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id,
                order_id=o_id, strategy_id=int(pipe["strategy_id"])
            ).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            "⚠️ По этой бумаге активны планы сразу в нескольких стратегиях. К какому плану относится этот ордер?",
            reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


async def _show_link_confirm(callback, p_id, t_id, l_id, o_id, strategy_id, strategy_name, current_step):
    confirm_text = generate_confirm_screen(
        header_text="🔗 **Привязка ордера к плану**",
        action_title="ПОДТВЕРЖДЕНИЕ",
        details_list=[
            f"Стратегия: {strategy_name}",
            f"Шаг плана: {current_step}",
            "Этот ордер будет считаться исполнением текущего шага.",
        ],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="✅ Да, привязать",
        yes_callback_packed=MenuAction(
            action="pipeline_link_execute", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id,
            order_id=o_id, strategy_id=strategy_id
        ).pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, sub_view="owner").pack()
    )
    try:
        await callback.message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "pipeline_link_confirm"))
async def process_link_confirm(callback: types.CallbackQuery, callback_data: MenuAction):
    """Промежуточный экран подтверждения при выборе среди нескольких активных планов."""
    p_id, t_id, l_id, o_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.order_id, callback_data.strategy_id
    await callback.answer()

    row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT op.current_step, s.strategy_name
            FROM public.order_pipelines op
            JOIN public.strategies s ON op.strategy_id = s.id
            WHERE op.portfolio_id = {int(p_id)} AND op.ticker_id = {int(t_id)} AND op.strategy_id = {int(s_id)}
              AND op.pipeline_status IN ('PENDING', 'ACTIVE') AND op.pending_broker_order_id IS NULL;
        """
    )
    if not row:
        await callback.answer("⚠️ План уже недоступен (возможно, шаг уже занят другим ордером).", show_alert=True)
        return

    await _show_link_confirm(callback, p_id, t_id, l_id, o_id, s_id, row["strategy_name"], int(row["current_step"]))


@router.callback_query(MenuAction.filter(F.action == "pipeline_link_execute"))
async def process_link_execute(callback: types.CallbackQuery, callback_data: MenuAction):
    """Проставляет pending_broker_order_id на текущий шаг существующего плана."""
    p_id, t_id, l_id, o_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.order_id, callback_data.strategy_id
    await callback.answer()

    order_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT broker_order_id FROM public.orders
            WHERE id = {int(o_id)} AND status IN ('active', 'NEW', 'PARTIALLY_FILLED')
              AND NOT EXISTS (SELECT 1 FROM public.order_pipelines op WHERE op.pending_broker_order_id = orders.broker_order_id);
        """
    )
    back_kb = _back_to_ticker_keyboard(p_id, l_id)
    if not order_row:
        try:
            await callback.message.edit_text("⚠️ Ордер уже неактивен или уже привязан. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    broker_order_id = order_row["broker_order_id"]

    updated = await asyncio.to_thread(
        db_sys.execute_query,
        f"""
            UPDATE public.order_pipelines
            SET pending_broker_order_id = '{broker_order_id}', updated_at = CURRENT_TIMESTAMP
            WHERE portfolio_id = {int(p_id)} AND ticker_id = {int(t_id)} AND strategy_id = {int(s_id)}
              AND pipeline_status IN ('PENDING', 'ACTIVE') AND pending_broker_order_id IS NULL
            RETURNING id;
        """
    )
    if not updated:
        try:
            await callback.message.edit_text("⚠️ Не удалось привязать -- план уже занят другим ордером. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    logging.info(f"🔗 [PIPELINE]: Ордер {broker_order_id} привязан к плану (стратегия #{s_id}, тикер {t_id}, портфель {p_id}).")
    try:
        await callback.message.edit_text(
            f"✅ Ордер №{broker_order_id} привязан к текущему шагу плана. Как только он исполнится, шаг будет засчитан автоматически.",
            reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass


# --- НОВЫЙ ПЛАН: ВЫБОР СТРАТЕГИИ УЖЕ СДЕЛАН, ЗАПРАШИВАЕМ ИТОГОВОЕ КОЛИЧЕСТВО ---

@router.callback_query(MenuAction.filter(F.action == "pipeline_new_strategy"))
async def process_new_strategy(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    p_id, t_id, l_id, o_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.order_id, callback_data.strategy_id
    await callback.answer()

    order_row = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT broker_order_id, q, p FROM public.orders
            WHERE id = {int(o_id)} AND status IN ('active', 'NEW', 'PARTIALLY_FILLED')
              AND NOT EXISTS (SELECT 1 FROM public.order_pipelines op WHERE op.pending_broker_order_id = orders.broker_order_id);
        """
    )
    back_kb = _back_to_ticker_keyboard(p_id, l_id)
    if not order_row:
        try:
            await callback.message.edit_text("⚠️ Ордер уже неактивен или уже привязан. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    prior_data = await state.get_data()
    await state.set_state(NewPipelineStates.waiting_for_target_quantity)
    await state.update_data(
        user_db_id=prior_data.get("user_db_id"),
        is_admin=prior_data.get("is_admin", False),
        new_pipeline_portfolio_id=p_id,
        new_pipeline_ticker_id=t_id,
        new_pipeline_listing_id=l_id,
        new_pipeline_order_id=o_id,
        new_pipeline_strategy_id=s_id,
        new_pipeline_broker_order_id=order_row["broker_order_id"],
        new_pipeline_step_qty=float(order_row["q"]),
        new_pipeline_entry_price=float(order_row["p"] or 0),
        menu_msg_id=callback.message.message_id,
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="pipeline_new_cancel", portfolio_id=p_id, listing_id=l_id).pack()))

    try:
        await callback.message.edit_text(
            f"🆕 **Новый план**\n\nЭтот ордер ({float(order_row['q']):.0f} шт по {float(order_row['p'] or 0):.2f}) -- шаг 1.\n"
            f"Отправьте в чат итоговое количество акций на ВЕСЬ план (все шаги суммарно).",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.message(NewPipelineStates.waiting_for_target_quantity)
async def process_target_quantity_text(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()
    raw = (message.text or "").strip().replace(",", ".")

    try:
        await message.delete()
    except Exception as del_err:
        logging.warning(f"⚠️ [НОВЫЙ ПЛАН]: Не удалось удалить сообщение пользователя: {del_err}")

    menu_msg_id = user_data.get("menu_msg_id")
    p_id = int(user_data.get("new_pipeline_portfolio_id", 0))
    l_id = int(user_data.get("new_pipeline_listing_id", 0))

    menu_message = types.Message(message_id=menu_msg_id, date=message.date, chat=message.chat, from_user=message.from_user)
    menu_message._bot = bot

    try:
        target_qty = float(raw)
    except ValueError:
        target_qty = 0.0

    step_qty = float(user_data.get("new_pipeline_step_qty", 0))
    if target_qty <= 0 or target_qty < step_qty:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="pipeline_new_cancel", portfolio_id=p_id, listing_id=l_id).pack()))
        await menu_message.edit_text(
            f"⚠️ Введите число больше {step_qty:.0f} (шаг 1 не может быть больше всего плана). Отправьте ещё раз.",
            reply_markup=builder.as_markup()
        )
        return

    await state.update_data(new_pipeline_target_quantity=target_qty)

    strat_row = await asyncio.to_thread(
        db_bot.execute_row, f"SELECT strategy_name FROM public.strategies WHERE id = {int(user_data.get('new_pipeline_strategy_id', 0))};"
    )
    strategy_name = (strat_row or {}).get("strategy_name", "?")

    confirm_text = generate_confirm_screen(
        header_text="🆕 **Новый план**",
        action_title="ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ",
        details_list=[
            f"Стратегия: {strategy_name}",
            f"Ордер №{user_data.get('new_pipeline_broker_order_id')} -- шаг 1: {step_qty:.0f} шт по {float(user_data.get('new_pipeline_entry_price', 0)):.2f}",
            f"Итого на весь план: {target_qty:.0f} шт",
        ],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, создать план",
        yes_callback_packed=MenuAction(action="pipeline_new_execute").pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="pipeline_new_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    )
    await menu_message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(MenuAction.filter(F.action == "pipeline_new_cancel"))
async def process_new_cancel(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    try:
        await callback.message.edit_text("Отменено.", reply_markup=_back_to_ticker_keyboard(p_id, l_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "pipeline_new_execute"))
async def process_new_execute(callback: types.CallbackQuery, state: FSMContext):
    """Создаёт новую строку order_pipelines: шаг 1, привязанный к уже выбранному ордеру."""
    user_data = await state.get_data()
    await callback.answer("🚀 Создаю план...")

    p_id = int(user_data.get("new_pipeline_portfolio_id", 0))
    t_id = int(user_data.get("new_pipeline_ticker_id", 0))
    l_id = int(user_data.get("new_pipeline_listing_id", 0))
    s_id = int(user_data.get("new_pipeline_strategy_id", 0))
    broker_order_id = user_data.get("new_pipeline_broker_order_id")
    entry_price = float(user_data.get("new_pipeline_entry_price", 0))
    target_qty = float(user_data.get("new_pipeline_target_quantity", 0))

    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    back_kb = _back_to_ticker_keyboard(p_id, l_id)

    if not broker_order_id or target_qty <= 0:
        try:
            await callback.message.edit_text("❌ Сбой: данные плана устарели. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    result = await asyncio.to_thread(
        db_sys.execute_query,
        f"""
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                ({p_id}, {l_id}, {t_id}, {s_id}, 1, 'PENDING',
                 {target_qty}, {entry_price}, '{broker_order_id}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """
    )
    if not result:
        logging.error(f"❌ [НОВЫЙ ПЛАН]: Сбой INSERT order_pipelines (портфель {p_id}, тикер {t_id}, стратегия {s_id}).")
        try:
            await callback.message.edit_text("❌ Сбой создания плана. Проверьте лог.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    pipe_id = result[0]["id"] if isinstance(result, list) else result["id"]
    logging.info(f"✅ [НОВЫЙ ПЛАН]: order_pipelines #{pipe_id} создан (портфель {p_id}, тикер {t_id}, стратегия {s_id}, ордер {broker_order_id}).")

    try:
        await callback.message.edit_text(
            f"✅ **План создан** (#{pipe_id})\nШаг 1 привязан к ордеру №{broker_order_id}. "
            f"Когда он исполнится, шаг будет засчитан автоматически.",
            parse_mode="Markdown",
            reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass
