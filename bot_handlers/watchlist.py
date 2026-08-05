import logging
import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

# Импортируем готовые объекты СУБД, фабрику и навигацию
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_watchlist_button_text, generate_main_menu_keyboard, generate_confirm_keyboard

router = Router()

@router.callback_query(MenuAction.filter(F.action == "show_watchlist_focus"))
async def process_watchlist_focus_menu(callback: types.CallbackQuery):
    """Шаг 2: Экран выбора фокуса исследования."""
    await callback.answer("Загрузка радаров исследования...")

    sql_portfolios = """
        SELECT p.id, p.name, u.name AS owner_name
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        ORDER BY p.id ASC;
    """
    p_res = await asyncio.to_thread(db_bot.execute_query, sql_portfolios)
    portfolios_list = p_res if isinstance(p_res, list) else ([p_res] if p_res else [])

    text = (
        "🔬 **СПИСКИ НАБЛЮДЕНИЯ СЕМЬИ**\n"
        "Выберите фокус исследования для примерки активов к инвест-стратегиям:\n"
    )

    builder = InlineKeyboardBuilder()

    for p in portfolios_list:
        p_id = int(p['id'])
        owner = p['owner_name']
        p_name = p['name']
        
        builder.row(types.InlineKeyboardButton(
            text=f"🔬 Фокус на {p_name} ({owner})",
            callback_data=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="assets").pack()
        ))

    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(generate_nav_back_keyboard(menu_only=True)))

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass

@router.callback_query(MenuAction.filter(F.action == "view_watchlist_portfolio"))
async def process_view_watchlist_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    """Шаг 3 и 4: Детализация листов наблюдения (Блок метаданных шапки)."""
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view or "assets"
    
    await callback.answer("Сборка паспорта исследования...")

    p_sql = """
        SELECT p.name AS portfolio_name, u.name AS owner_name, p.strategy_type
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        WHERE p.id = %s;
    """
    p_meta_res = db_bot.execute_query(p_sql, (p_id,))
    p_meta = p_meta_res if isinstance(p_meta_res, list) else ([p_meta_res] if p_meta_res else [])

    if p_meta and len(p_meta) > 0:
        p_row = p_meta[0] if isinstance(p_meta, list) else p_meta
        owner_name = p_row.get('owner_name', 'Unknown')
        portfolio_name = p_row.get('portfolio_name', f"П{p_id}")
        strategy_type = p_row.get('strategy_type', 'Not Set')
    else:
        owner_name = "Unknown"
        portfolio_name = f"П{p_id}"
        strategy_type = "Not Set"

    report_text = f"🔬 **ФОКУС НА {portfolio_name.upper()}**\n"
    report_text += f"👤 {owner_name}\n"
    report_text += f"💼 `{strategy_type}`\n"
    report_text += f"───────\n"

    builder = InlineKeyboardBuilder()

    # 3. ЛОГИКА ОТРЕНДЕРИВАНИЯ ВНУТРЕННОСТЕЙ ШТОРОК
    if view == "assets":
        report_text += "🎯 **АКТИВНЫЕ РАДАРЫ И ЦЕЛИ СЛЕДОВАНИЯ:**"
        
        # 🔥 СУПЕР-ЗАПРОС АГРЕГАЦИИ С ВЫГРУЗКОЙ ТЕКУЩЕЙ ЦЕНЫ БРОКЕРА (last_price)
        watchlist_query = """
            SELECT w.id, l.id AS listing_id, l.broker_symbol, t.symbol, t.company_name, l.last_price,
                   w.ordered_at, w.bought_at, w.sold_out_at,
                   COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count,
                   COUNT(DISTINCT op.id)::int as active_plans_count,
                   COUNT(DISTINCT sa.strategy_id)::int as strategy_count
            FROM public.watchlist w
            JOIN public.listings l ON w.listing_id = l.id
            JOIN public.tickers t ON l.ticker_id = t.id
            LEFT JOIN public.alerts al ON al.listing_id = w.listing_id
                                      AND al.portfolio_id = w.portfolio_id
                                      AND al.is_active = true
            LEFT JOIN public.order_pipelines op ON op.portfolio_id = w.portfolio_id AND op.listing_id = w.listing_id
                                      AND op.pipeline_status IN ('PENDING', 'ACTIVE')
            LEFT JOIN public.assets a ON a.portfolio_id = w.portfolio_id AND a.listing_id = w.listing_id
            LEFT JOIN public.strategy_assets sa ON sa.asset_id = a.id AND sa.allocated_quantity > 0
            WHERE w.portfolio_id = %s
            GROUP BY w.id, l.id, t.symbol, t.company_name, l.last_price
            ORDER BY t.symbol ASC;
        """

        w_res_raw = db_bot.execute_query(watchlist_query, (p_id,))
        w_res = w_res_raw if isinstance(w_res_raw, list) else ([w_res_raw] if w_res_raw else [])

        if w_res:
            for item in w_res:
                pure_symbol = item['symbol']
                l_id = int(item['listing_id'] or 0)
                last_price = float(item['last_price'] or 0)
                
                # Многомерный радар фаз жизненного цикла UPort. Фазы "Изучение"/"Наблюдение"
                # (considered_at/watched_at) убраны из радара 2026-07-30 -- на живых данных
                # watched_at заполнен всегда (0 NULL из 39 строк), бейдж не различал ничего;
                # considered_at не пишется ни одним живым интерактивным путём (см. BACKLOG.md
                # п.12/47), только тремя легаси-инсертами в sync_account_fb.py, всегда синхронно
                # с watched_at -- то есть никогда не нёс отдельной информации.
                b_order = "📃" if item.get('ordered_at') is not None else ""
                b_asset = "💼" if item.get('bought_at') is not None else ""
                b_sold  = "🏁" if item.get('sold_out_at') is not None else ""
                # Отдельный от жизненного цикла факт -- прямо сейчас есть активный План
                # (order_pipelines PENDING/ACTIVE), см. Claude/11_asset_lifecycle_and_plan.md
                b_plan = "📋" if int(item.get('active_plans_count') or 0) > 0 else ""

                # В скольких стратегиях портфеля бумага реально держится сейчас
                # (strategy_assets.allocated_quantity > 0) -- по просьбе пользователя
                # 2026-07-30, между "Портфель" и "Распродано".
                strategy_count = int(item.get('strategy_count') or 0)
                b_strategy = "🎯" if strategy_count > 0 else ""

                # Извлекаем честный счетчик активных алертов из базы
                alerts_count = int(item.get('active_alerts_count') or 0)
                # Логика видимости колокольчика вынесена ВНЕ генератора по вашему ТЗ
                b_alert = "🔔" if alerts_count > 0 else ""

                # 🔥 РЕФАКТОРИНГ: Переводим Списки наблюдения на жесткую LEGO-сетку с радаром
                button_text = generate_watchlist_button_text(
                    ticker=pure_symbol,
                    f1=b_order,
                    f2=b_asset,
                    f_strategy=b_strategy,
                    strategy_count=strategy_count,
                    f3=b_sold,
                    f4=b_plan,
                    alert_icon=b_alert,
                    alerts_count=alerts_count
                )

                # Полная карточка тикера (sub_view="owner"), не сразу шторка алертов --
                # по просьбе пользователя (2026-07-29), чтобы была видна кнопка "План".
                builder.row(types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, ticker_name=pure_symbol, sub_view="owner").pack()
                ))
        else:
            report_text += "\n   *Активы в данном списке наблюдения пока отсутствуют.*"

    elif view == "yahoo":
        report_text += "📊 **Усредненные фундаментальные показатели радара (Yahoo):**\n"
        report_text += "   *Здесь будет Сводный анализ мультипликаторов для этого списка наблюдения.*"
    
    report_text += f"\n───────\n"

    if view == "assets":
        builder.row(types.InlineKeyboardButton(
            text="📊 Открыть метрики Yahoo", 
            callback_data=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="yahoo").pack()
        ))
    elif view == "yahoo":
        builder.row(types.InlineKeyboardButton(
            text="📦 Показать состав исследования", 
            callback_data=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="assets").pack()
        ))
        
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(generate_nav_back_keyboard(
        one_step_back_text="🔙 К выбору фокуса",
        full_back_callback=MenuAction(action="show_watchlist_focus").pack()
    )))

    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass

# =========================================================================
# 🔥 КОНТУР ВНЕДРЕНИЯ ИНВЕСТ-ИДЕЙ v1.0 (ИНТЕЛЛЕКТУАЛЬНЫЙ РАДАР WATCHLIST)
# =========================================================================
from aiogram.fsm.context import FSMContext

@router.callback_query(MenuAction.filter(F.action == "add_to_wl"))
async def process_add_to_watchlist_routing(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Этап А: Перехват клика "В список наблюдения".
    Сканирует торговые портфели инвестора. Реализует смарт-разводку mono/multi-портфелей.
    """
    await callback.answer()
    
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    # 🔥 ФИКС: Считываем чистый, уникальный ticker_id из фабрики без AttributeError!
    t_id = callback_data.ticker_id

    if not user_db_id or not t_id:
        await callback.message.edit_text("❌ Критическая ошибка сессии. Пройдите /start.", reply_markup=generate_main_menu_keyboard())
        return

    sql_get_portfolios = """
        SELECT id, name FROM public.portfolios
        WHERE owner_id = %s AND id != 0
        ORDER BY id ASC;
    """
    p_res = await asyncio.to_thread(db_bot.execute_query, sql_get_portfolios, (user_db_id,))
    user_portfolios = p_res if isinstance(p_res, list) else ([p_res] if p_res else [])

    if not user_portfolios:
        await callback.message.edit_text("⚠️ У вас нет зарегистрированных торговых портфелей.", reply_markup=generate_main_menu_keyboard())
        return

    # 🔹 СЦЕНАРИЙ 1: У пользователя ровно ОДИН портфель — штампуем в один клик
    if len(user_portfolios) == 1:
        p_row = user_portfolios[0] if isinstance(user_portfolios, list) else user_portfolios
        target_p_id = int(p_row["id"])
        logging.debug(f"🧬 [WATCHLIST]: Моно-портфель обнаружен. Авто-выбор portfolio_id = {target_p_id}")
        
        await execute_watchlist_fixation(callback.message, target_p_id, t_id, origin=callback_data.sub_view)
        return

    # 🔹 СЦЕНАРИЙ 2: Несколько портфелей — выводим лаконичное меню выбора портфеля
    builder = InlineKeyboardBuilder()
    for p in user_portfolios:
        # Передаем выбранный portfolio_id и сохраняем исследуемый ticker_id в кнопке подтверждения
        builder.row(types.InlineKeyboardButton(
            text=f"💼 {p['name']}",
            callback_data=MenuAction(action="confirm_wl_add", portfolio_id=int(p["id"]), ticker_id=int(t_id), sub_view=callback_data.sub_view).pack()
        ))
    
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(generate_nav_back_keyboard(menu_only=True)))

    try:
        await callback.message.edit_text("Выберите портфель:", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "confirm_wl_add"))
async def process_confirm_watchlist_addition(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Этап Б: Фиксация выбора в мульти-портфельном режиме.
    """
    await callback.answer("Легализую актив...")
    await execute_watchlist_fixation(callback.message, callback_data.portfolio_id, callback_data.ticker_id, origin=callback_data.sub_view)


async def execute_watchlist_fixation(message: types.Message, portfolio_id: int, ticker_id: int, origin: str = ""):
    """
    Атомарный системный исполнитель v2.0
    Легализует листинг через изолированный метод ensure_listing и штампует статус в ватчлист.

    origin -- откуда реально пришли (составной sub_view, тот же приём, что уже решил
    аналогичную проблему для шторки алертов, см. BACKLOG.md "Сделано" п.17): "digest"
    (кнопка из раздела дайджеста), "search" (кнопка из паспорта тикера, глобальный
    поиск -- см. bot_handlers/ticker_search.py::process_view_ticker_passport) или ""
    (нераспознанное/отсутствующее происхождение -- только "В главное меню", как и
    было раньше). Влияет только на кнопку "назад" на финальном экране успеха, см. ниже.
    """
    import logging
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    try:
        # 1. Извлекаем данные тикера для красивого финального вывода
        sql_get_ticker = "SELECT symbol, (ticker_name_map->>'YAHOO') as yahoo_symbol FROM public.tickers WHERE id = %s LIMIT 1;"
        t_data = db_sys.execute_query(sql_get_ticker, (ticker_id,))
        t_row = t_data[0] if t_data and isinstance(t_data, list) else (t_data if isinstance(t_data, dict) else {})
        
        if not t_row:
            await message.edit_text("❌ Ошибка: Ticker ID не найден в СУБД.", reply_markup=generate_main_menu_keyboard())
            return
            
        raw_symbol = t_row["symbol"]
        yahoo_symbol = t_row.get("yahoo_symbol", raw_symbol)

        # 2. Вычисляем ID брокера, к которому привязан выбранный портфель
        sql_get_broker = "SELECT broker_id FROM public.portfolios WHERE id = %s LIMIT 1;"
        p_data = db_sys.execute_query(sql_get_broker, (portfolio_id,))
        p_row = p_data[0] if p_data and isinstance(p_data, list) else (p_data if isinstance(p_data, dict) else {})
        target_broker_id = int(p_row["broker_id"]) if p_row and p_row.get("broker_id") else 1

        logging.debug(f"🧱 [WATCHLIST]: Легализация листинга для T_ID={ticker_id} (Portfolio: {portfolio_id}, Broker: {target_broker_id})...")
        
        # 3. 🔥 ВЫЗЫВАЕМ НАШ НОВЫЙ ИЗОЛИРОВАННЫЙ МЕТОД ЛИСТИНГОВ (Без захода в паспортистку!)
        # Метод сам зряче проверит кэш, найдет имя брокера и снимет котировку последней сделки.
        l_id = db_sys.ensure_listing(
            ticker_id=int(ticker_id),
            broker_id=target_broker_id,
            fb_client=None
        )

        if not l_id or l_id == 0:
            await message.edit_text("❌ Критическая ошибка: Не удалось легализовать листинг брокера для ватчлиста.")
            return

        # 4. АТОМАРНАЯ ЗАПИСЬ В ТАБЛИЦУ WATCHLIST
        db_sys.ensure_watchlist_row_v2(portfolio_id=int(portfolio_id), listing_id=int(l_id), reason="watched")
        
        # 5. ВЫВОД ФИНАЛЬНОГО ПРЕМИАЛЬНОГО СТАТУСА -- кнопка "назад" зависит от origin
        if origin == "digest":
            success_markup = generate_nav_back_keyboard(
                one_step_back_text="🔙 Назад к дайджесту",
                full_back_callback=MenuAction(action="view_digest", portfolio_id=portfolio_id, sub_view="signals").pack()
            )
        elif origin == "search":
            success_markup = generate_nav_back_keyboard(
                one_step_back_text="🔙 Назад к поиску",
                full_back_callback=MenuAction(action="view_ticker_passport", ticker_id=ticker_id).pack()
            )
        else:
            success_markup = generate_nav_back_keyboard(menu_only=True)

        await message.edit_text(f"✅ Добавлено: **{yahoo_symbol}**", parse_mode="Markdown", reply_markup=success_markup)
        logging.info(f"🏁 [WATCHLIST]: Бумага {yahoo_symbol} добавлена в список наблюдения (портфель {portfolio_id}).")

    except ValueError as val_err:
        # Перехватываем наш кастомный шлагбаум безопасности ("Этот инструмент не торгуется...")
        logging.warning(f"⚠️ [WATCHLIST LIBS BLOCK]: {val_err}")
        await message.edit_text(f"❌ {val_err}", reply_markup=generate_nav_back_keyboard(menu_only=True))

    except Exception as fix_err:
        logging.error(f"🚨 [WATCHLIST CRITICAL СБОЙ]: Ошибка фиксации инвест-идеи: {fix_err}")
        await message.edit_text("❌ Сбой записи актива в списки наблюдения СУБД.", reply_markup=generate_main_menu_keyboard())


# =========================================================================
# 🗑 УДАЛЕНИЕ ИЗ СН -- только там, где известен конкретный портфель (карточка
# тикера, "owner"-режим), см. обсуждение 2026-07-30 в Claude/BACKLOG.md.
# =========================================================================

def get_watchlist_removal_status(db_instance, portfolio_id: int, listing_id: int) -> dict | None:
    """
    Проверяет, есть ли бумага в СН этого КОНКРЕТНОГО портфеля и безопасно ли её оттуда
    убрать -- те же поля, что уже считаются для LEGO-радара СН (bot_handlers/watchlist.py,
    process_view_watchlist_portfolio): нет активного приказа, бумага не куплена (не
    держится), не привязана ни к одной стратегии, нет активного Плана (order_pipelines
    PENDING/ACTIVE), нет активных алертов брокера. Возвращает None, если записи в СН
    для этой пары (портфель, листинг) вообще нет.
    """
    row = db_instance.execute_row("""
        SELECT w.id, w.ordered_at, w.bought_at,
               COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count,
               COUNT(DISTINCT op.id)::int as active_plans_count,
               COUNT(DISTINCT sa.strategy_id)::int as strategy_count
        FROM public.watchlist w
        LEFT JOIN public.alerts al ON al.listing_id = w.listing_id
                                   AND al.portfolio_id = w.portfolio_id AND al.is_active = true
        LEFT JOIN public.order_pipelines op ON op.portfolio_id = w.portfolio_id AND op.listing_id = w.listing_id
                                   AND op.pipeline_status IN ('PENDING', 'ACTIVE')
        LEFT JOIN public.assets a ON a.portfolio_id = w.portfolio_id AND a.listing_id = w.listing_id
        LEFT JOIN public.strategy_assets sa ON sa.asset_id = a.id AND sa.allocated_quantity > 0
        WHERE w.portfolio_id = %s AND w.listing_id = %s
        GROUP BY w.id, w.ordered_at, w.bought_at;
    """, (portfolio_id, listing_id))
    if not row:
        return None

    is_safe = (
        row.get("ordered_at") is None
        and row.get("bought_at") is None
        and int(row.get("strategy_count") or 0) == 0
        and int(row.get("active_plans_count") or 0) == 0
        and int(row.get("active_alerts_count") or 0) == 0
    )
    return {"watchlist_id": row["id"], "is_safe": is_safe}


@router.callback_query(MenuAction.filter(F.action == "confirm_remove_wl"))
async def process_confirm_remove_watchlist(callback: types.CallbackQuery, callback_data: MenuAction):
    """Шаг подтверждения -- кнопка в футере карточки тикера показывается, только если уже безопасно, но перепроверяем перед показом на случай, если состояние изменилось за секунду до клика."""
    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    symbol = callback_data.ticker_name or "бумагу"

    status = await asyncio.to_thread(get_watchlist_removal_status, db_bot, p_id, l_id)
    back_callback = MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, ticker_name=symbol, sub_view="owner").pack()

    if not status or not status["is_safe"]:
        await callback.answer("⚠️ Состояние изменилось -- бумага больше не годится для удаления из СН.", show_alert=True)
        return

    await callback.answer()
    keyboard = generate_confirm_keyboard(
        yes_text="🗑 Да, убрать из СН",
        yes_callback_packed=MenuAction(action="execute_remove_wl", portfolio_id=p_id, listing_id=l_id, ticker_name=symbol).pack(),
        no_text="❌ Отмена",
        no_callback_packed=back_callback,
    )
    try:
        await callback.message.edit_text(f"Убрать **{symbol}** из списка наблюдения этого портфеля?", parse_mode="Markdown", reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "execute_remove_wl"))
async def process_execute_remove_watchlist(callback: types.CallbackQuery, callback_data: MenuAction):
    """Собственно удаление -- перепроверяет безопасность заново прямо перед DELETE (не доверяет состоянию с прошлого экрана)."""
    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    symbol = callback_data.ticker_name or "Бумага"

    status = await asyncio.to_thread(get_watchlist_removal_status, db_bot, p_id, l_id)
    if not status or not status["is_safe"]:
        await callback.answer("⚠️ Состояние изменилось -- удаление отменено.", show_alert=True)
        return

    await asyncio.to_thread(db_sys.execute_query, "DELETE FROM public.watchlist WHERE id = %s;", (status['watchlist_id'],))
    await callback.answer("Убрано из списка наблюдения.")

    logging.info(f"🗑 [WATCHLIST REMOVE]: {symbol} убран из СН портфеля {p_id} (watchlist_id={status['watchlist_id']}).")

    reply_markup = generate_nav_back_keyboard(
        one_step_back_text="🔬 К списку наблюдения",
        full_back_callback=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="assets").pack()
    )

    try:
        await callback.message.edit_text(f"✅ **{symbol}** убран из списка наблюдения.", parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass
