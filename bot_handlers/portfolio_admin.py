import asyncio
import logging
from aiogram import Router, types, F, Bot
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_screens import generate_confirm_screen
from bot_handlers.bot_keyboards import generate_confirm_keyboard, generate_nav_back_keyboard

router = Router()


class NewPortfolioStates(StatesGroup):
    waiting_for_name = State()


def _admin_only(user_data: dict) -> bool:
    return bool(user_data.get("is_admin", False))


# --- ЭКРАН 1: ВЫБОР ВЛАДЕЛЬЦА ---

@router.callback_query(MenuAction.filter(F.action == "admin_portfolio_new"))
async def process_pick_owner(callback: types.CallbackQuery, state: FSMContext):
    """Служебное действие администратора: старт создания нового портфеля -- выбор владельца."""
    user_data = await state.get_data()
    if not _admin_only(user_data):
        await callback.answer("🛑 Отказано в доступе.", show_alert=True)
        return

    await callback.answer()

    sql_users = "SELECT id, name FROM public.users WHERE role != 'ai' ORDER BY name;"
    users = await asyncio.to_thread(db_bot.execute_query, sql_users)
    users = users if isinstance(users, list) else []

    builder = InlineKeyboardBuilder()
    for u in users:
        builder.row(types.InlineKeyboardButton(
            text=f"👤 {u['name']}",
            callback_data=MenuAction(action="admin_portfolio_pick_broker", user_id=int(u['id'])).pack()
        ))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="settings_main").pack()))

    await callback.message.edit_text(
        "➕ **Создание портфеля**\n\nШаг 1 из 3: выберите владельца портфеля.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


# --- ЭКРАН 2: ВЫБОР БРОКЕРА ---

@router.callback_query(MenuAction.filter(F.action == "admin_portfolio_pick_broker"))
async def process_pick_broker(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Шаг 2: выбор брокера для уже выбранного владельца."""
    user_data = await state.get_data()
    if not _admin_only(user_data):
        await callback.answer("🛑 Отказано в доступе.", show_alert=True)
        return

    await callback.answer()
    owner_id = callback_data.user_id

    sql_brokers = "SELECT id, name FROM public.brokers ORDER BY name;"
    brokers = await asyncio.to_thread(db_bot.execute_query, sql_brokers)
    brokers = brokers if isinstance(brokers, list) else []

    builder = InlineKeyboardBuilder()
    for b in brokers:
        builder.row(types.InlineKeyboardButton(
            text=f"🏦 {b['name']}",
            callback_data=MenuAction(action="admin_portfolio_name_prompt", user_id=owner_id, broker_id=int(b['id'])).pack()
        ))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="settings_main").pack()))

    await callback.message.edit_text(
        "➕ **Создание портфеля**\n\nШаг 2 из 3: выберите брокера.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


# --- ЭКРАН 3: ВВОД ИМЕНИ (FSM) ---

@router.callback_query(MenuAction.filter(F.action == "admin_portfolio_name_prompt"))
async def process_name_prompt(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Шаг 3: запрашивает у администратора имя нового портфеля текстом."""
    user_data = await state.get_data()
    if not _admin_only(user_data):
        await callback.answer("🛑 Отказано в доступе.", show_alert=True)
        return

    await callback.answer()

    await state.set_state(NewPortfolioStates.waiting_for_name)
    await state.update_data(
        new_portfolio_owner_id=callback_data.user_id,
        new_portfolio_broker_id=callback_data.broker_id,
        menu_msg_id=callback.message.message_id,
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="admin_portfolio_cancel").pack()))

    await callback.message.edit_text(
        "➕ **Создание портфеля**\n\nШаг 3 из 3: отправьте в чат имя нового портфеля.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@router.message(NewPortfolioStates.waiting_for_name)
async def process_name_text(message: types.Message, state: FSMContext, bot: Bot):
    """Ловит текст имени портфеля и показывает экран подтверждения."""
    user_data = await state.get_data()
    if not _admin_only(user_data):
        await state.clear()
        return

    raw_name = (message.text or "").strip()

    try:
        await message.delete()
    except Exception as del_err:
        logging.warning(f"⚠️ [СОЗДАНИЕ ПОРТФЕЛЯ]: Не удалось удалить сообщение пользователя: {del_err}")

    menu_msg_id = user_data.get("menu_msg_id")
    owner_id = int(user_data.get("new_portfolio_owner_id", 0))
    broker_id = int(user_data.get("new_portfolio_broker_id", 0))

    menu_message = types.Message(
        message_id=menu_msg_id,
        date=message.date,
        chat=message.chat,
        from_user=message.from_user
    )
    menu_message._bot = bot

    if not raw_name:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="admin_portfolio_cancel").pack()))
        await menu_message.edit_text(
            "⚠️ Имя портфеля не может быть пустым. Отправьте текст ещё раз.",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        return

    await state.update_data(new_portfolio_name=raw_name)

    sql_owner = "SELECT name FROM public.users WHERE id = %s LIMIT 1;"
    sql_broker = "SELECT name FROM public.brokers WHERE id = %s LIMIT 1;"
    owner_row = await asyncio.to_thread(db_bot.execute_row, sql_owner, (owner_id,))
    broker_row = await asyncio.to_thread(db_bot.execute_row, sql_broker, (broker_id,))
    owner_name = owner_row.get("name", f"ID {owner_id}")
    broker_name = broker_row.get("name", f"ID {broker_id}")

    confirm_text = generate_confirm_screen(
        header_text="➕ **Создание портфеля**",
        action_title="ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ",
        details_list=[
            f"Имя: {raw_name}",
            f"Владелец: {owner_name}",
            f"Брокер: {broker_name}",
            "При создании автоматически добавятся все 5 стратегий: Кэш/Резерв (10%) и "
            "Неопределённая (90%) активны сразу; Револьверная/Консервативное накопление/"
            "Трендовая заводятся ПАССИВНЫМИ с долей 0% -- как готовые цели будущего "
            "переноса, включаются вручную, когда понадобятся (см. Claude/BACKLOG.md п.9)",
        ],
        parse_mode="Markdown"
    )

    reply_markup = generate_confirm_keyboard(
        yes_text="🚀 Да, создать портфель",
        yes_callback_packed=MenuAction(action="admin_portfolio_execute").pack(),
        no_text="❌ Отменить",
        no_callback_packed=MenuAction(action="admin_portfolio_cancel").pack()
    )

    await menu_message.edit_text(confirm_text, parse_mode="Markdown", reply_markup=reply_markup)


@router.callback_query(MenuAction.filter(F.action == "admin_portfolio_cancel"))
async def process_cancel(callback: types.CallbackQuery, state: FSMContext):
    """
    Отмена на любом шаге создания портфеля. По образцу process_back_to_menu (summary.py):
    бережно сохраняем системные user_db_id/is_admin через clear() + update_data(),
    остальные временные ключи (new_portfolio_*, menu_msg_id) отбрасываются вместе с очисткой.
    """
    user_data = await state.get_data()
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)

    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    # process_settings_main сам вызывает callback.answer() -- здесь не отвечаем повторно,
    # Telegram не позволяет отвечать на один и тот же callback дважды.
    from bot_handlers.settings import process_settings_main
    await process_settings_main(callback, state)


# --- ЭКРАН 4: ИСПОЛНЕНИЕ ---

@router.callback_query(MenuAction.filter(F.action == "admin_portfolio_execute"))
async def process_execute(callback: types.CallbackQuery, state: FSMContext):
    """Атомарно создаёт портфель и обе служебные стратегии (Кэш/Резерв + Неопределенная)."""
    user_data = await state.get_data()
    if not _admin_only(user_data):
        await callback.answer("🛑 Отказано в доступе.", show_alert=True)
        return

    await callback.answer("🚀 Создаю портфель...")

    owner_id = int(user_data.get("new_portfolio_owner_id", 0))
    broker_id = int(user_data.get("new_portfolio_broker_id", 0))
    raw_name = user_data.get("new_portfolio_name", "")

    # Атомарно заводит ВСЕ 5 шаблонов стратегий (не только служебные), см.
    # Claude/BACKLOG.md п.9 (2026-07-31) -- каждый портфель должен иметь полный
    # набор, а не только те стратегии, которые сегодня кажутся нужными (будущая
    # реструктуризация непредсказуема). Кэш/Резерв и Неопределённая -- активны
    # сразу с заводской долей (`recommended_share_pct`); содержательные
    # (Револьверная/Консервативное накопление/Трендовая) -- Пассивные, доля 0%,
    # включаются вручную, когда понадобятся (готовая цель будущего переноса).
    sql_create = """
        WITH new_portfolio AS (
            INSERT INTO public.portfolios (name, owner_id, broker_id)
            VALUES (%s, %s, %s)
            RETURNING id
        ),
        all_strategies AS (
            INSERT INTO public.strategies
                (portfolio_id, template_id, strategy_name, rules_config, human_philosophy,
                 strategy_share_pct, is_active, is_screening_active)
            SELECT
                new_portfolio.id, st.id, st.template_name, st.rules_config, st.human_philosophy,
                CASE WHEN st.system_key IN ('CASH_RESERVE', 'UNALLOCATED')
                     THEN st.recommended_share_pct ELSE 0.00 END,
                true,
                CASE WHEN st.system_key IN ('CASH_RESERVE', 'UNALLOCATED') THEN true ELSE false END
            FROM new_portfolio, public.strategy_templates st
            RETURNING id, template_id
        ),
        seeded_tactics AS (
            -- Шаги лесенки входа -- свойство ТИПА стратегии, копируются из шаблонного
            -- strategy_template_tactics (Claude/BACKLOG.md, 2026-08-13 -- раньше эта
            -- проводка отсутствовала, strategy_tactics у новых портфелей оставалась
            -- пустой, LadderStepWatcher для них молчал не по замыслу, а по нехватке
            -- данных; Индексное ядро/Кэш-Резерв/Неопределённая тактик не имеют --
            -- у ядра нет лесенки по конструкции, у служебных стратегий нет покупок).
            INSERT INTO public.strategy_tactics (strategy_id, step_number, budget_share_pct, trigger_conditions)
            SELECT a.id, tt.step_number, tt.budget_share_pct, tt.trigger_conditions
            FROM all_strategies a
            JOIN public.strategy_template_tactics tt ON tt.template_id = a.template_id
        )
        SELECT
            new_portfolio.id AS portfolio_id,
            (SELECT a.id FROM all_strategies a JOIN public.strategy_templates st ON a.template_id = st.id
                WHERE st.system_key = 'CASH_RESERVE') AS cash_strategy_id,
            (SELECT a.id FROM all_strategies a JOIN public.strategy_templates st ON a.template_id = st.id
                WHERE st.system_key = 'UNALLOCATED') AS unalloc_strategy_id
        FROM new_portfolio;
    """

    result = await asyncio.to_thread(db_sys.execute_query, sql_create, (raw_name, owner_id, broker_id))

    # Бережно сохраняем системные user_db_id/is_admin через clear() + update_data(),
    # как и в process_cancel/process_back_to_menu -- иначе админ незаметно потеряет
    # доступ к панели администратора до следующего /start.
    is_admin = user_data.get("is_admin", False)
    user_db_id = user_data.get("user_db_id", None)
    await state.clear()
    await state.update_data(user_db_id=user_db_id, is_admin=is_admin)

    if not result or not isinstance(result, list) or "portfolio_id" not in result[0]:
        logging.error(f"❌ [СОЗДАНИЕ ПОРТФЕЛЯ]: Сбой атомарного INSERT: {result}")
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="settings_main").pack()))
        await callback.message.edit_text(
            "❌ **Сбой создания портфеля!**\nПроверьте лог `uport_system.log`.",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        return

    row = result[0]
    p_id = int(row["portfolio_id"])
    cash_id = int(row["cash_strategy_id"])
    unalloc_id = int(row["unalloc_strategy_id"])

    logging.info(f"✅ [СОЗДАНИЕ ПОРТФЕЛЯ]: Портфель #{p_id} создан (Кэш/Резерв #{cash_id}, Неопределенная #{unalloc_id}, + 3 пассивные содержательные стратегии).")

    await callback.message.edit_text(
        f"✅ **Портфель успешно создан!**\n\n"
        f"🆔 Портфель: #{p_id}\n"
        f"💰 Кэш/Резерв: #{cash_id} (10%)\n"
        f"📥 Неопределенная стратегия: #{unalloc_id} (90%)\n"
        f"😴 Револьверная/Консервативное накопление/Трендовая — добавлены пассивными (0%), включите вручную по мере надобности",
        parse_mode="Markdown",
        reply_markup=generate_nav_back_keyboard(menu_only=True)
    )
