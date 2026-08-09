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
from bot_handlers.bot_utils import execute_virtual_transfer
from analytics.analytics_utils import TickerEvaluator
from brokers_connectors.sync_strategy_asset_fb import SyncStrategyAssetFB

router = Router()

# Содержательные стратегии, для которых вообще имеет смысл план входа (см. Claude/03_strategies_and_templates.md)
CONTENT_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING", "INDEX_CORE")


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

    sql = """
        SELECT o.id, o.broker_order_id, o.q, o.p, o.oper
        FROM public.orders o
        WHERE o.portfolio_id = %s AND o.ticker_id = %s
          AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED')
          AND NOT EXISTS (
              SELECT 1 FROM public.order_pipelines op
              WHERE op.pending_broker_order_id = o.broker_order_id
          )
        ORDER BY o.created_at DESC;
    """
    orders = await asyncio.to_thread(db_bot.execute_query, sql, (int(p_id), int(t_id)))
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

    sql_pipelines = """
        SELECT op.strategy_id, op.current_step, s.strategy_name
        FROM public.order_pipelines op
        JOIN public.strategies s ON op.strategy_id = s.id
        WHERE op.portfolio_id = %s AND op.ticker_id = %s
          AND op.pipeline_status IN ('PENDING', 'ACTIVE')
          AND op.pending_broker_order_id IS NULL;
    """
    pipelines = await asyncio.to_thread(db_bot.execute_query, sql_pipelines, (int(p_id), int(t_id)))
    pipelines = pipelines if isinstance(pipelines, list) else ([pipelines] if pipelines else [])

    back_kb = _back_to_ticker_keyboard(p_id, l_id)

    if not pipelines:
        # Нет ни одного плана, ждущего шаг по этой бумаге -- заводим новый (или все уже ждут другой ордер)
        # st.system_key IN CONTENT_SYSTEM_KEYS -- фиксированный жёстко зашитый кортеж (не из
        # пользовательского ввода), безопасен как f-строка -- placeholder не годится для
        # структурного IN-списка (см. Claude/BACKLOG.md №81).
        sql_strategies = f"""
            SELECT strategy_id AS id, strategy_name
            FROM public.v_strategies_full
            WHERE portfolio_id = %s AND is_active = true
              AND system_key IN {CONTENT_SYSTEM_KEYS}
            ORDER BY display_order;
        """
        strategies = await asyncio.to_thread(db_bot.execute_query, sql_strategies, (int(p_id),))
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
        """
            SELECT op.current_step, s.strategy_name
            FROM public.order_pipelines op
            JOIN public.strategies s ON op.strategy_id = s.id
            WHERE op.portfolio_id = %s AND op.ticker_id = %s AND op.strategy_id = %s
              AND op.pipeline_status IN ('PENDING', 'ACTIVE') AND op.pending_broker_order_id IS NULL;
        """,
        (int(p_id), int(t_id), int(s_id))
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
        """
            SELECT broker_order_id FROM public.orders
            WHERE id = %s AND status IN ('active', 'NEW', 'PARTIALLY_FILLED')
              AND NOT EXISTS (SELECT 1 FROM public.order_pipelines op WHERE op.pending_broker_order_id = orders.broker_order_id);
        """,
        (int(o_id),)
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
        """
            UPDATE public.order_pipelines
            SET pending_broker_order_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE portfolio_id = %s AND ticker_id = %s AND strategy_id = %s
              AND pipeline_status IN ('PENDING', 'ACTIVE') AND pending_broker_order_id IS NULL
            RETURNING id;
        """,
        (broker_order_id, int(p_id), int(t_id), int(s_id))
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
        """
            SELECT broker_order_id, q, p FROM public.orders
            WHERE id = %s AND status IN ('active', 'NEW', 'PARTIALLY_FILLED')
              AND NOT EXISTS (SELECT 1 FROM public.order_pipelines op WHERE op.pending_broker_order_id = orders.broker_order_id);
        """,
        (int(o_id),)
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
        db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(user_data.get('new_pipeline_strategy_id', 0)),)
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
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 1, 'PENDING',
                 %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (p_id, l_id, t_id, s_id, target_qty, entry_price, broker_order_id)
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


# =========================================================================
# 🌱 "ПЛАН ВХОДА" -- ПРОАКТИВНОЕ СОЗДАНИЕ ПЛАНА ДО ВСЯКОЙ ПОКУПКИ
# (см. Claude/11_asset_lifecycle_and_plan.md, "рождение из идеи" -- ветка 1 из трёх;
# ветки 2/3, когда бумага уже чем-то держится, пока не реализованы -- нужен реальный
# кейс для теста, см. BACKLOG.md). Создаёт "голый" order_pipelines без привязанного
# ордера -- process_link_order выше уже ищет именно такие планы (pending_broker_order_id
# IS NULL), поэтому дальнейшая привязка ордера отрабатывает существующим кодом без правок.
# =========================================================================

class NewIdeaPlanStates(StatesGroup):
    waiting_for_target_quantity = State()


def _back_to_plan_keyboard(portfolio_id: int, listing_id: int):
    return generate_nav_back_keyboard(
        one_step_back_text="🔙 К плану",
        full_back_callback=MenuAction(action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id, sub_view="plan").pack()
    )


@router.callback_query(MenuAction.filter(F.action == "plan_from_idea_start"))
async def process_plan_from_idea_start(callback: types.CallbackQuery, callback_data: MenuAction):
    """Показывает список содержательных стратегий портфеля -- та же выборка, что и в fallback-ветке process_link_order."""
    p_id = callback_data.portfolio_id
    t_id = callback_data.ticker_id
    l_id = callback_data.listing_id
    await callback.answer()

    # st.system_key IN CONTENT_SYSTEM_KEYS -- фиксированный жёстко зашитый кортеж, см. комментарий выше
    sql_strategies = f"""
        SELECT strategy_id AS id, strategy_name
        FROM public.v_strategies_full
        WHERE portfolio_id = %s AND is_active = true
          AND system_key IN {CONTENT_SYSTEM_KEYS}
        ORDER BY display_order;
    """
    strategies = await asyncio.to_thread(db_bot.execute_query, sql_strategies, (int(p_id),))
    strategies = strategies if isinstance(strategies, list) else ([strategies] if strategies else [])

    back_kb = _back_to_plan_keyboard(p_id, l_id)

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
                action="plan_from_idea_strategy", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id, strategy_id=int(s["id"])
            ).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            "📝 **План входа**\n\nВыберите стратегию:", parse_mode="Markdown", reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "plan_from_idea_strategy"))
async def process_plan_from_idea_strategy(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Стратегия выбрана. Проверяем текущее владение (три ветки рождения, см.
    Claude/11_asset_lifecycle_and_plan.md) -- но не по "владеем ли вообще чем-то", а по
    "владеем ли в ДРУГОЙ стратегии": если бумага уже держится в ТОЙ ЖЕ стратегии (пусть и
    по давно завершённому циклу) или не куплена вовсе -- это всё ещё ветка 1, новый
    независимый цикл поверх старого. Настоящая ветка 3 (перенос между стратегиями) --
    только если бумага сейчас реально числится в ДРУГОЙ стратегии -- ведём в тот же единый
    механизм переноса (transfer_*), что и с прямой кнопки на карточке тикера, а не строим
    вторую отдельную реализацию (согласовано 2026-07-30).
    """
    p_id, t_id, l_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.strategy_id
    await callback.answer()

    other_strategy_row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT sa.strategy_id, sa.allocated_quantity, s.strategy_name
            FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            JOIN public.strategies s ON sa.strategy_id = s.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s
              AND sa.strategy_id != %s AND sa.allocated_quantity > 0
            LIMIT 1;
        """,
        (int(p_id), int(l_id), int(s_id))
    )

    if other_strategy_row:
        target_row = await asyncio.to_thread(db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(s_id),))
        target_name = (target_row or {}).get("strategy_name", "?")
        await _start_transfer_quantity_ask(
            callback, state, p_id, t_id, l_id,
            source_id=int(other_strategy_row["strategy_id"]), target_id=s_id, target_name=target_name,
            source_qty=float(other_strategy_row["allocated_quantity"])
        )
        return

    prior_data = await state.get_data()
    await state.set_state(NewIdeaPlanStates.waiting_for_target_quantity)
    await state.update_data(
        user_db_id=prior_data.get("user_db_id"),
        is_admin=prior_data.get("is_admin", False),
        idea_plan_portfolio_id=p_id,
        idea_plan_ticker_id=t_id,
        idea_plan_listing_id=l_id,
        idea_plan_strategy_id=s_id,
        menu_msg_id=callback.message.message_id,
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="🔙 Отмена", callback_data=MenuAction(action="plan_from_idea_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    ))

    try:
        await callback.message.edit_text(
            "📝 **План входа**\n\nЕщё ничего не куплено. Отправьте в чат итоговое количество акций на ВЕСЬ план "
            "(все шаги суммарно) -- покупать пока ничего не нужно, план будет ждать вашей покупки в терминале брокера.",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.message(NewIdeaPlanStates.waiting_for_target_quantity)
async def process_idea_plan_quantity_text(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()
    raw = (message.text or "").strip().replace(",", ".")

    try:
        await message.delete()
    except Exception as del_err:
        logging.warning(f"⚠️ [ПЛАН ВХОДА]: Не удалось удалить сообщение пользователя: {del_err}")

    menu_msg_id = user_data.get("menu_msg_id")
    p_id = int(user_data.get("idea_plan_portfolio_id", 0))
    l_id = int(user_data.get("idea_plan_listing_id", 0))

    menu_message = types.Message(message_id=menu_msg_id, date=message.date, chat=message.chat, from_user=message.from_user)
    menu_message._bot = bot

    try:
        target_qty = float(raw)
    except ValueError:
        target_qty = 0.0

    if target_qty <= 0:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="🔙 Отмена", callback_data=MenuAction(action="plan_from_idea_cancel", portfolio_id=p_id, listing_id=l_id).pack()
        ))
        await menu_message.edit_text(
            "⚠️ Введите число больше 0. Отправьте ещё раз.",
            reply_markup=builder.as_markup()
        )
        return

    await state.update_data(idea_plan_target_quantity=target_qty)

    strat_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(user_data.get('idea_plan_strategy_id', 0)),)
    )
    strategy_name = (strat_row or {}).get("strategy_name", "?")

    confirm_text = generate_confirm_screen(
        header_text="📝 **План входа**",
        action_title="ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ",
        details_list=[
            f"Стратегия: {strategy_name}",
            f"Итого на весь план: {target_qty:.0f} шт",
            "Ничего ещё не куплено -- план будет ждать вашей покупки в терминале брокера.",
        ],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, создать план",
        yes_callback_packed=MenuAction(action="plan_from_idea_execute").pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="plan_from_idea_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    )
    await menu_message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(MenuAction.filter(F.action == "plan_from_idea_cancel"))
async def process_idea_plan_cancel(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    try:
        await callback.message.edit_text("Отменено.", reply_markup=_back_to_plan_keyboard(p_id, l_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "plan_from_idea_execute"))
async def process_idea_plan_execute(callback: types.CallbackQuery, state: FSMContext):
    """
    Создаёт "голый" план -- шаг 1, без pending_broker_order_id. Уже совместим с
    process_link_order (тот ищет планы именно с pending_broker_order_id IS NULL) --
    правок в реконсиляции/привязке ордера не требуется.
    """
    user_data = await state.get_data()
    await callback.answer("🚀 Создаю план...")

    p_id = int(user_data.get("idea_plan_portfolio_id", 0))
    t_id = int(user_data.get("idea_plan_ticker_id", 0))
    l_id = int(user_data.get("idea_plan_listing_id", 0))
    s_id = int(user_data.get("idea_plan_strategy_id", 0))
    target_qty = float(user_data.get("idea_plan_target_quantity", 0))

    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if target_qty <= 0:
        try:
            await callback.message.edit_text("❌ Сбой: данные плана устарели. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    result = await asyncio.to_thread(
        db_sys.execute_query,
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 1, 'PENDING',
                 %s, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (p_id, l_id, t_id, s_id, target_qty)
    )
    if not result:
        logging.error(f"❌ [ПЛАН ВХОДА]: Сбой INSERT order_pipelines (портфель {p_id}, тикер {t_id}, стратегия {s_id}).")
        try:
            await callback.message.edit_text("❌ Сбой создания плана. Проверьте лог.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    pipe_id = result[0]["id"] if isinstance(result, list) else result["id"]
    logging.info(f"✅ [ПЛАН ВХОДА]: order_pipelines #{pipe_id} создан без ордера (портфель {p_id}, тикер {t_id}, стратегия {s_id}).")

    try:
        await callback.message.edit_text(
            f"✅ **План создан** (#{pipe_id})\nПокупайте в терминале брокера, когда будете готовы -- "
            f"потом привяжите ордер через «🔗 Привязать ордер к плану».",
            parse_mode="Markdown",
            reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass


# =========================================================================
# 📉 "ПЛАН ВЫХОДА" -- ПРОАКТИВНОЕ СОЗДАНИЕ ПЛАНА ПРОДАЖИ (симметрично "Плану входа")
# (см. Claude/11_asset_lifecycle_and_plan.md, "Смерть" -- лесенка выхода). Заводит
# order_pipelines с ОТРИЦАТЕЛЬНЫМ target_quantity -- _find_target_strategy уже умеет
# сопоставлять дельту по знаку ("Знак сохраняется: >0 вход, <0 выход"), правок в
# реконсиляции не требуется. На сегодня -- один шаг (продать сразу нужное количество),
# лесенка частичных продаж -- отдельный открытый вопрос (см. BACKLOG.md).
# =========================================================================

class ExitPlanStates(StatesGroup):
    waiting_for_sell_quantity = State()


@router.callback_query(MenuAction.filter(F.action == "plan_exit_start"))
async def process_plan_exit_start(callback: types.CallbackQuery, callback_data: MenuAction):
    """Показывает стратегии, в которых бумага реально держится (allocated_quantity > 0)."""
    p_id = callback_data.portfolio_id
    t_id = callback_data.ticker_id
    l_id = callback_data.listing_id
    await callback.answer()

    sql_holdings = """
        SELECT s.id, s.strategy_name, sa.allocated_quantity
        FROM public.strategy_assets sa
        JOIN public.assets a ON sa.asset_id = a.id
        JOIN public.strategies s ON sa.strategy_id = s.id
        WHERE a.portfolio_id = %s AND a.listing_id = %s
          AND sa.allocated_quantity > 0;
    """
    holdings = await asyncio.to_thread(db_bot.execute_query, sql_holdings, (p_id, l_id))
    holdings = holdings if isinstance(holdings, list) else ([holdings] if holdings else [])

    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if not holdings:
        try:
            await callback.message.edit_text(
                "⚠️ Эта бумага сейчас не держится ни в одной стратегии этого портфеля.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
        return

    builder = InlineKeyboardBuilder()
    for h in holdings:
        builder.row(types.InlineKeyboardButton(
            text=f"🎯 {h['strategy_name']} ({float(h['allocated_quantity']):.0f} шт)",
            callback_data=MenuAction(
                action="plan_exit_strategy", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id, strategy_id=int(h["id"])
            ).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            "📋 **План выхода**\n\nВыберите стратегию, из которой продаёте:", parse_mode="Markdown", reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "plan_exit_strategy"))
async def process_plan_exit_strategy(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Стратегия выбрана -- запоминаем текущий остаток в ней (верхняя граница для ввода количества)."""
    p_id, t_id, l_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.strategy_id
    await callback.answer()

    holding_row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT sa.allocated_quantity FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.strategy_id = %s;
        """,
        (p_id, l_id, s_id)
    )
    held_qty = float((holding_row or {}).get("allocated_quantity") or 0)
    if held_qty <= 0:
        await callback.answer("⚠️ В этой стратегии уже нечего продавать.", show_alert=True)
        return

    prior_data = await state.get_data()
    await state.set_state(ExitPlanStates.waiting_for_sell_quantity)
    await state.update_data(
        user_db_id=prior_data.get("user_db_id"),
        is_admin=prior_data.get("is_admin", False),
        exit_plan_portfolio_id=p_id,
        exit_plan_ticker_id=t_id,
        exit_plan_listing_id=l_id,
        exit_plan_strategy_id=s_id,
        exit_plan_held_qty=held_qty,
        menu_msg_id=callback.message.message_id,
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="🔙 Отмена", callback_data=MenuAction(action="plan_exit_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    ))

    try:
        await callback.message.edit_text(
            f"📋 **План выхода**\n\nВ этой стратегии сейчас {held_qty:.0f} шт. Отправьте в чат количество, "
            f"которое хотите продать (не больше {held_qty:.0f}) -- продавать пока ничего не нужно, план будет "
            f"ждать вашей продажи в терминале брокера.",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.message(ExitPlanStates.waiting_for_sell_quantity)
async def process_exit_plan_quantity_text(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()
    raw = (message.text or "").strip().replace(",", ".")

    try:
        await message.delete()
    except Exception as del_err:
        logging.warning(f"⚠️ [ПЛАН ВЫХОДА]: Не удалось удалить сообщение пользователя: {del_err}")

    menu_msg_id = user_data.get("menu_msg_id")
    p_id = int(user_data.get("exit_plan_portfolio_id", 0))
    l_id = int(user_data.get("exit_plan_listing_id", 0))
    held_qty = float(user_data.get("exit_plan_held_qty", 0))

    menu_message = types.Message(message_id=menu_msg_id, date=message.date, chat=message.chat, from_user=message.from_user)
    menu_message._bot = bot

    try:
        sell_qty = float(raw)
    except ValueError:
        sell_qty = 0.0

    if sell_qty <= 0 or sell_qty > held_qty:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="🔙 Отмена", callback_data=MenuAction(action="plan_exit_cancel", portfolio_id=p_id, listing_id=l_id).pack()
        ))
        await menu_message.edit_text(
            f"⚠️ Введите число больше 0 и не больше {held_qty:.0f}. Отправьте ещё раз.",
            reply_markup=builder.as_markup()
        )
        return

    await state.update_data(exit_plan_sell_quantity=sell_qty)

    strat_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(user_data.get('exit_plan_strategy_id', 0)),)
    )
    strategy_name = (strat_row or {}).get("strategy_name", "?")

    confirm_text = generate_confirm_screen(
        header_text="📋 **План выхода**",
        action_title="ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ",
        details_list=[
            f"Стратегия: {strategy_name}",
            f"Продать: {sell_qty:.0f} шт из {held_qty:.0f}",
            "Ничего ещё не продано -- план будет ждать вашей продажи в терминале брокера.",
        ],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, создать план",
        yes_callback_packed=MenuAction(action="plan_exit_execute").pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="plan_exit_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    )
    await menu_message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(MenuAction.filter(F.action == "plan_exit_cancel"))
async def process_exit_plan_cancel(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    try:
        await callback.message.edit_text("Отменено.", reply_markup=_back_to_plan_keyboard(p_id, l_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "plan_exit_execute"))
async def process_exit_plan_execute(callback: types.CallbackQuery, state: FSMContext):
    """
    Создаёт "голый" план выхода -- шаг 1, ОТРИЦАТЕЛЬНОЕ target_quantity, без
    pending_broker_order_id. Уже совместим с process_link_order/_find_target_strategy
    без единой правки -- знак там уже давно учитывается.
    """
    user_data = await state.get_data()
    await callback.answer("🚀 Создаю план...")

    p_id = int(user_data.get("exit_plan_portfolio_id", 0))
    t_id = int(user_data.get("exit_plan_ticker_id", 0))
    l_id = int(user_data.get("exit_plan_listing_id", 0))
    s_id = int(user_data.get("exit_plan_strategy_id", 0))
    sell_qty = float(user_data.get("exit_plan_sell_quantity", 0))

    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if sell_qty <= 0:
        try:
            await callback.message.edit_text("❌ Сбой: данные плана устарели. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    result = await asyncio.to_thread(
        db_sys.execute_query,
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 1, 'PENDING',
                 %s, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (p_id, l_id, t_id, s_id, -sell_qty)
    )
    if not result:
        logging.error(f"❌ [ПЛАН ВЫХОДА]: Сбой INSERT order_pipelines (портфель {p_id}, тикер {t_id}, стратегия {s_id}).")
        try:
            await callback.message.edit_text(
                "❌ Сбой создания плана. Возможно, по этой стратегии уже есть активный план (входа или выхода) -- "
                "сначала заверши или отмени его.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
        return

    pipe_id = result[0]["id"] if isinstance(result, list) else result["id"]
    logging.info(f"✅ [ПЛАН ВЫХОДА]: order_pipelines #{pipe_id} создан без ордера, target={-sell_qty} (портфель {p_id}, тикер {t_id}, стратегия {s_id}).")

    try:
        await callback.message.edit_text(
            f"✅ **План выхода создан** (#{pipe_id})\nПродавайте в терминале брокера, когда будете готовы -- "
            f"потом привяжите ордер через «🔗 Привязать ордер к плану».",
            parse_mode="Markdown",
            reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass


# =========================================================================
# 🔄 "ПЕРЕНОС" -- ЕДИНЫЙ МЕХАНИЗМ ПЕРЕНОСА МЕЖДУ СТРАТЕГИЯМИ (согласовано 2026-07-30,
# см. Claude/11_asset_lifecycle_and_plan.md, "реинкарнация"). Две двери в один и тот же
# код: прямая кнопка на карточке тикера (transfer_start), и обнаружение "держится в
# другой стратегии" внутри "Плана входа" (process_plan_from_idea_strategy выше) --
# оба ведут в _start_transfer_quantity_ask, не в две параллельные реализации.
# Перенос в СОДЕРЖАТЕЛЬНУЮ стратегию всегда сопровождается планом (переносим -- значит,
# уже есть решение, куда и зачем); перенос в служебную (пока только "Неопределённая",
# "Кэш/Резерв" как приёмник исключён -- бессмысленно для реальной позиции в бумаге) --
# просто перенос, без плана, планировать там нечего.
# =========================================================================

class TransferPlanStates(StatesGroup):
    waiting_for_target_quantity = State()


async def _get_current_holders(portfolio_id: int, listing_id: int) -> list:
    rows = await asyncio.to_thread(
        db_bot.execute_query,
        """
            SELECT sa.strategy_id, s.strategy_name, sa.allocated_quantity
            FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            JOIN public.strategies s ON sa.strategy_id = s.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s
              AND sa.allocated_quantity > 0;
        """,
        (portfolio_id, listing_id)
    )
    return rows if isinstance(rows, list) else ([rows] if rows else [])


@router.callback_query(MenuAction.filter(F.action == "transfer_start"))
async def process_transfer_start(callback: types.CallbackQuery, callback_data: MenuAction):
    """Вход с карточки тикера. Если бумага держится в нескольких стратегиях сразу -- сначала спрашиваем источник."""
    p_id, t_id, l_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id
    await callback.answer()

    holders = await _get_current_holders(p_id, l_id)
    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if not holders:
        try:
            await callback.message.edit_text(
                "⚠️ Эта бумага сейчас не держится ни в одной стратегии этого портфеля.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
        return

    if len(holders) == 1:
        h = holders[0]
        await _show_transfer_target_picker(callback, p_id, t_id, l_id, int(h["strategy_id"]), h["strategy_name"], float(h["allocated_quantity"]))
        return

    builder = InlineKeyboardBuilder()
    for h in holders:
        builder.row(types.InlineKeyboardButton(
            text=f"🎯 {h['strategy_name']} ({float(h['allocated_quantity']):.0f} шт)",
            callback_data=MenuAction(
                action="transfer_source", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id, strategy_id=int(h["strategy_id"])
            ).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            "🔄 **Перенос**\n\nБумага держится сразу в нескольких стратегиях -- откуда переносим?",
            parse_mode="Markdown", reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "transfer_source"))
async def process_transfer_source(callback: types.CallbackQuery, callback_data: MenuAction):
    """Источник выбран (только когда держателей было несколько) -- дальше общий выбор приёмника."""
    p_id, t_id, l_id, s_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id, callback_data.strategy_id
    await callback.answer()

    row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT sa.allocated_quantity, s.strategy_name FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            JOIN public.strategies s ON sa.strategy_id = s.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.strategy_id = %s;
        """,
        (p_id, l_id, s_id)
    )
    if not row:
        await callback.answer("⚠️ Уже не актуально.", show_alert=True)
        return

    await _show_transfer_target_picker(callback, p_id, t_id, l_id, s_id, row["strategy_name"], float(row["allocated_quantity"]))


async def _show_transfer_target_picker(callback, p_id: int, t_id: int, l_id: int, source_id: int, source_name: str, source_qty: float):
    """
    Список стратегий-приёмников (исключая источник) с подсказкой совместимости (✅/❌)
    для содержательных стратегий -- переиспользует уже существующий
    TickerEvaluator.evaluate_ticker_strategy (та же проверка, что уже показывается в
    паспорте тикера при глобальном поиске).

    «Кэш/Резерв» исключён из приёмников не произвольно, а по природе вещи: это служебная
    стратегия, моделирующая ДЕНЬГИ (доля капитала, не отдельная таблица счетов, см.
    Claude/03_strategies_and_templates.md) -- в неё в принципе нельзя перенести долю
    актива, она физически не может содержать позиции по бумагам.
    """
    strategies = await asyncio.to_thread(
        db_bot.execute_query,
        """
            SELECT s.id, s.strategy_name, st.system_key
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = %s AND s.is_active = true
              AND s.id != %s AND st.system_key != 'CASH_RESERVE';
        """,
        (p_id, source_id)
    )
    strategies = strategies if isinstance(strategies, list) else ([strategies] if strategies else [])
    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if not strategies:
        try:
            await callback.message.edit_text("⚠️ В портфеле нет других стратегий-приёмников.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    evaluator = TickerEvaluator(db_instance=db_bot)
    report = await asyncio.to_thread(evaluator.evaluate_ticker_strategy, t_id, p_id)
    explain_map = report.get("explain_map", {})

    builder = InlineKeyboardBuilder()
    for s in strategies:
        s_id = int(s["id"])
        badge = ""
        if s["system_key"] in CONTENT_SYSTEM_KEYS:
            info = explain_map.get(s_id)
            if info:
                badge = " ✅" if info["is_compatible_technically"] else " ❌"
        builder.row(types.InlineKeyboardButton(
            text=f"🎯 {s['strategy_name']}{badge}",
            callback_data=MenuAction(
                action="transfer_target", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id,
                strategy_id=source_id, task_id=s_id
            ).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))

    try:
        await callback.message.edit_text(
            f"🔄 **Перенос**\n\nИз «{source_name}» ({source_qty:.0f} шт) — куда перенести?",
            parse_mode="Markdown", reply_markup=final_builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "transfer_target"))
async def process_transfer_target(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Приёмник выбран. Содержательная стратегия -- просим количество на весь план и
    дальше заводим план вместе с переносом; служебная («Неопределённая») -- просто
    переносим, планировать там нечего.
    """
    p_id, t_id, l_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id
    source_id, target_id = callback_data.strategy_id, callback_data.task_id
    await callback.answer()

    row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT sa.allocated_quantity FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.strategy_id = %s;
        """,
        (p_id, l_id, source_id)
    )
    source_qty = float((row or {}).get("allocated_quantity") or 0)
    if source_qty <= 0:
        await callback.answer("⚠️ Уже не актуально -- в источнике не осталось акций.", show_alert=True)
        return

    target_row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT s.strategy_name, st.system_key FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.id = %s;
        """,
        (target_id,)
    )
    if not target_row:
        await callback.answer("⚠️ Стратегия больше не существует.", show_alert=True)
        return

    if target_row["system_key"] in CONTENT_SYSTEM_KEYS:
        await _start_transfer_quantity_ask(callback, state, p_id, t_id, l_id, source_id, target_id, target_row["strategy_name"], source_qty)
    else:
        await _show_transfer_confirm_simple(callback, p_id, t_id, l_id, source_id, target_id, target_row["strategy_name"], source_qty)


async def _start_transfer_quantity_ask(callback, state: FSMContext, p_id: int, t_id: int, l_id: int, source_id: int, target_id: int, target_name: str, source_qty: float):
    """Общая точка для обеих дверей (прямая кнопка "Перенос" и обнаружение внутри "Плана входа")."""
    prior_data = await state.get_data()
    await state.set_state(TransferPlanStates.waiting_for_target_quantity)
    await state.update_data(
        user_db_id=prior_data.get("user_db_id"),
        is_admin=prior_data.get("is_admin", False),
        transfer_portfolio_id=p_id,
        transfer_ticker_id=t_id,
        transfer_listing_id=l_id,
        transfer_source_id=source_id,
        transfer_target_id=target_id,
        transfer_source_qty=source_qty,
        menu_msg_id=callback.message.message_id,
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="🔙 Отмена", callback_data=MenuAction(action="transfer_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    ))

    try:
        await callback.message.edit_text(
            f"🔄 **Перенос в «{target_name}»**\n\nСейчас переносим {source_qty:.0f} шт. Отправьте в чат итоговое "
            f"количество на ВЕСЬ план в этой стратегии (все шаги суммарно, включая уже переносимое).",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.message(TransferPlanStates.waiting_for_target_quantity)
async def process_transfer_quantity_text(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()
    raw = (message.text or "").strip().replace(",", ".")

    try:
        await message.delete()
    except Exception as del_err:
        logging.warning(f"⚠️ [ПЕРЕНОС]: Не удалось удалить сообщение пользователя: {del_err}")

    menu_msg_id = user_data.get("menu_msg_id")
    p_id = int(user_data.get("transfer_portfolio_id", 0))
    l_id = int(user_data.get("transfer_listing_id", 0))
    source_qty = float(user_data.get("transfer_source_qty", 0))

    menu_message = types.Message(message_id=menu_msg_id, date=message.date, chat=message.chat, from_user=message.from_user)
    menu_message._bot = bot

    try:
        target_qty = float(raw)
    except ValueError:
        target_qty = 0.0

    if target_qty < source_qty:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="🔙 Отмена", callback_data=MenuAction(action="transfer_cancel", portfolio_id=p_id, listing_id=l_id).pack()
        ))
        await menu_message.edit_text(
            f"⚠️ Введите число не меньше {source_qty:.0f} (переносимое количество не может превышать план). Отправьте ещё раз.",
            reply_markup=builder.as_markup()
        )
        return

    await state.update_data(transfer_target_quantity=target_qty)

    target_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(user_data.get('transfer_target_id', 0)),)
    )
    source_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(user_data.get('transfer_source_id', 0)),)
    )
    target_name = (target_row or {}).get("strategy_name", "?")
    source_name = (source_row or {}).get("strategy_name", "?")

    confirm_text = generate_confirm_screen(
        header_text="🔄 **Перенос**",
        action_title="ПОДТВЕРЖДЕНИЕ",
        details_list=[
            f"Перенести {source_qty:.0f} шт из «{source_name}» в «{target_name}»",
            f"Итого на план в «{target_name}»: {target_qty:.0f} шт",
        ],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, перенести и создать план",
        yes_callback_packed=MenuAction(action="transfer_execute").pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="transfer_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    )
    await menu_message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(MenuAction.filter(F.action == "transfer_cancel"))
async def process_transfer_cancel(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    p_id, l_id = callback_data.portfolio_id, callback_data.listing_id
    try:
        await callback.message.edit_text("Отменено.", reply_markup=_back_to_plan_keyboard(p_id, l_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "transfer_execute"))
async def process_transfer_execute(callback: types.CallbackQuery, state: FSMContext):
    """
    Переносит доли (execute_virtual_transfer) и заводит "голый" план в целевой стратегии,
    сразу же прогоняя перенесённое количество через уже существующую логику сопоставления
    шагов (_find_target_strategy) -- план должен оказаться на правильном шаге/статусе, а
    не притворяться, что ничего ещё не куплено (см. Claude/11_asset_lifecycle_and_plan.md).
    """
    user_data = await state.get_data()
    await callback.answer("🚀 Переношу и создаю план...")

    p_id = int(user_data.get("transfer_portfolio_id", 0))
    t_id = int(user_data.get("transfer_ticker_id", 0))
    l_id = int(user_data.get("transfer_listing_id", 0))
    source_id = int(user_data.get("transfer_source_id", 0))
    target_id = int(user_data.get("transfer_target_id", 0))
    source_qty = float(user_data.get("transfer_source_qty", 0))
    target_qty = float(user_data.get("transfer_target_quantity", 0))

    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    back_kb = _back_to_plan_keyboard(p_id, l_id)

    if source_qty <= 0 or target_qty <= 0:
        try:
            await callback.message.edit_text("❌ Сбой: данные устарели. Начните заново.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    # 1. Физический перенос долей между стратегиями (уже существующий, ранее не
    # подключённый ни к одной живой кнопке механизм).
    await execute_virtual_transfer(
        portfolio_id=p_id, listing_id=l_id, source_strategy_id=source_id, target_strategy_id=target_id, quantity=source_qty
    )

    # 2. Заводим "голый" план в целевой стратегии -- шаг 1, PENDING, без ордера.
    result = await asyncio.to_thread(
        db_sys.execute_query,
        """
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 1, 'PENDING',
                 %s, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id;
        """,
        (p_id, l_id, t_id, target_id, target_qty)
    )
    if not result:
        logging.error(f"❌ [ПЕРЕНОС]: Перенос выполнен, но сбой INSERT order_pipelines (портфель {p_id}, тикер {t_id}, стратегия {target_id}).")
        try:
            await callback.message.edit_text(
                "⚠️ Перенос выполнен, но план создать не удалось (возможно, уже есть активный план в этой стратегии). Проверьте лог.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
        return
    pipe_id = result[0]["id"] if isinstance(result, list) else result["id"]

    # 3. Прогоняем перенесённое количество через ту же математику, что и реальная
    # рыночная дельта -- план сразу окажется на правильном шаге, если перенесённого
    # количества уже достаточно (не перезаписываем баланс повторно -- перенос его уже
    # применил, здесь только определяем current_step/pipeline_status).
    sync = SyncStrategyAssetFB(db_sys)
    _, matched_pipe_id, action, _, _ = await asyncio.to_thread(sync._find_target_strategy, p_id, t_id, source_qty)

    if matched_pipe_id == pipe_id and action in ('NEXT_STEP', 'COMPLETE_PIPELINE'):
        await asyncio.to_thread(sync._update_pipeline_status, pipe_id, action)
        step_summary = "План уже полностью выполнен перенесённым количеством." if action == 'COMPLETE_PIPELINE' else "Шаг 1 плана уже засчитан перенесённым количеством."
    else:
        step_summary = "План ждёт первого шага (перенесённого количества пока недостаточно)."

    logging.info(f"✅ [ПЕРЕНОС]: {source_qty} шт перенесено из стратегии {source_id} в {target_id}, план #{pipe_id} создан (портфель {p_id}, тикер {t_id}).")

    try:
        await callback.message.edit_text(
            f"✅ **Перенос выполнен** (план #{pipe_id})\n{source_qty:.0f} шт перенесено. {step_summary}",
            parse_mode="Markdown",
            reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass


async def _show_transfer_confirm_simple(callback, p_id: int, t_id: int, l_id: int, source_id: int, target_id: int, target_name: str, source_qty: float):
    """Перенос в служебную стратегию (сейчас -- только «Неопределённая») -- без плана, планировать там нечего."""
    source_row = await asyncio.to_thread(db_bot.execute_row, "SELECT strategy_name FROM public.strategies WHERE id = %s;", (int(source_id),))
    source_name = (source_row or {}).get("strategy_name", "?")

    confirm_text = generate_confirm_screen(
        header_text="🔄 **Перенос**",
        action_title="ПОДТВЕРЖДЕНИЕ",
        details_list=[f"Перенести {source_qty:.0f} шт из «{source_name}» в «{target_name}»"],
        parse_mode="Markdown"
    )
    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, перенести",
        yes_callback_packed=MenuAction(
            action="transfer_execute_simple", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id,
            strategy_id=source_id, task_id=target_id
        ).pack(),
        no_text="❌ Отмена",
        no_callback_packed=MenuAction(action="transfer_cancel", portfolio_id=p_id, listing_id=l_id).pack()
    )
    try:
        await callback.message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "transfer_execute_simple"))
async def process_transfer_execute_simple(callback: types.CallbackQuery, callback_data: MenuAction):
    """Перенос без плана (приёмник -- служебная стратегия)."""
    p_id, t_id, l_id = callback_data.portfolio_id, callback_data.ticker_id, callback_data.listing_id
    source_id, target_id = callback_data.strategy_id, callback_data.task_id
    await callback.answer("🚀 Переношу...")

    row = await asyncio.to_thread(
        db_bot.execute_row,
        """
            SELECT sa.allocated_quantity FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.strategy_id = %s;
        """,
        (p_id, l_id, source_id)
    )
    qty = float((row or {}).get("allocated_quantity") or 0)
    back_kb = _back_to_plan_keyboard(p_id, l_id)
    if qty <= 0:
        try:
            await callback.message.edit_text("⚠️ Уже не актуально.", reply_markup=back_kb)
        except TelegramBadRequest:
            pass
        return

    await execute_virtual_transfer(
        portfolio_id=p_id, listing_id=l_id, source_strategy_id=source_id, target_strategy_id=target_id, quantity=qty
    )
    logging.info(f"✅ [ПЕРЕНОС]: {qty} шт перенесено из стратегии {source_id} в {target_id} без плана (портфель {p_id}, тикер {t_id}).")

    try:
        await callback.message.edit_text(f"✅ Перенос выполнен: {qty:.0f} шт.", reply_markup=back_kb)
    except TelegramBadRequest:
        pass
