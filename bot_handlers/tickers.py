import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_screens import (
    format_premium_header, format_position_financials, format_market_signals, format_ticker_behavior,
    format_cross_holdings, format_broker_link_summary, format_order_line,
    format_alert_condition_line, classify_order, ORDER_CATEGORIES, SEPARATOR_LINE,
)
from bot_handlers.bot_keyboards import generate_nav_back_keyboard
#from bot_handlers.summary import get_back_to_menu_keyboard, execute_sql_async

# Импортируем наш аналитический модуль аудита лимитов под стратегию (IPS)
from analytics.portfolio_auditor import audit_ticker_for_portfolio

# Инициализируем локальный роутер для модуля карточек акций
router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_ticker"))
async def process_view_ticker(callback: types.CallbackQuery, callback_data: MenuAction):
    """Экран Уровня 3: Детализация конкретной бумаги с глубоким дебагом."""
    
    # 🧪 КРИТИЧЕСКИЙ ДЕБАГ №1: Смотрим, что физически прилетело из кнопки Telegram
    print(f"\n📥 [ДЕБАГ КЛИКА ТИКЕРА]: Сработал хэндлер!")
    print(f"   • callback_data: {callback_data}")
    print(f"   • Считанный portfolio_id: {callback_data.portfolio_id}")
    print(f"   • Считанный sub_view: {callback_data.sub_view}")
    
    # Безопасно проверяем наличие полей в объекте фабрики
    l_id = 0
    if hasattr(callback_data, 'listing_id'):
        l_id = callback_data.listing_id or 0
    elif 'listing_id' in callback_data.__dict__:
        l_id = callback_data.__dict__['listing_id'] or 0

    p_id = callback_data.portfolio_id
        
    t_name = getattr(callback_data, 'ticker_name', '') or ''
    view = callback_data.sub_view or "owner"
    strategy_id = getattr(callback_data, 'strategy_id', 0) or 0  # Контекст "пришли из карточки стратегии" для кнопки "назад"

    # Контекст "откуда пришли в шторку алертов" (BACKLOG.md №31): по умолчанию watchlist
    # (единственный путь входа исторически, watchlist.py его не меняет), но если пришли
    # из самой карточки тикера (Блок 5), sub_view приходит как "alerts/from_ticker" --
    # тот же приём составного sub_view, что и в strategy_resolver.py ("transfer/N/N").
    alerts_origin = "watchlist"
    if view.startswith("alerts"):
        parts = view.split("/", 1)
        view = parts[0]
        if len(parts) > 1:
            alerts_origin = parts[1]

    print(f"   • Итоговые переменные после парсинга: l_id={l_id} | ticker_name='{t_name}' | view='{view}'")

    # 1. РЕЛЯЦИОННОЕ ОПРЕДЕЛЕНИЕ ТИКЕРА И ПЛОЩАДКИ БРОКЕРА
    if l_id > 0:
        # Сценарий А: Вход из инлайн-кнопок портфеля по числовому ID листинга
        listing_sql = f"""
            SELECT l.broker_symbol, l.ticker_id, t.symbol, t.company_name, l.currency_id, l.last_price
            FROM public.listings l
            JOIN public.tickers t ON l.ticker_id = t.id
            WHERE l.id = {l_id};
        """
        print(f"   • 📡 [ДЕБАГ SQL]: Отправляю запрос по листингу {l_id}...")
        # 🔥 РЕФАКТОРИНГ: Заменяем на execute_row, убирая кашу проверок на списки/словари
        l_row = db_bot.execute_row(listing_sql)
        print(f"   • [ДЕБАГ СУБД ОТВЕТ]: {l_row}")
        
        if not l_row:
            print("   • ❌ СУБД вернула пустоту для этого l_id!")
            await callback.answer("❌ Листинг актива не найден в СУБД.", show_alert=True)
            return

        t_id = int(l_row['ticker_id'])
        pure_symbol = l_row['symbol']
        broker_symbol = l_row['broker_symbol']
        last_price = float(l_row['last_price'] or 0)
        currency_id = l_row['currency_id']
        print(f"   • ✅ УСПЕШНО РАСПАРСЕНО: {pure_symbol} (ID: {t_id})")
    else:
        # Сценарий Б: Вход по текстовому поиску чистого глобального тикера из Телеграм
        t_name = callback_data.ticker_name.strip().upper()
        
        # 🔥 КАСКАДНЫЙ ИНТЕЛЛЕКТ v3.0: Прогоняем через универсальный автономный шлюз СУБД с проверкой кэша!
        try:
            # Импортируем db_sys для безопасной легализации и записи новой бумаги, если её нет в кэше
            from database import db_sys
            
            # Переключаем алерты и поиск бота на Главные Ворота экосистемы UPort с ролью TG_USR
            t_id, l_id = db_sys.ensure_ticker_v3(
                ticker_name_raw=t_name, 
                caller_role="TG_USR", 
                caller_id=None, 
                broker_id=1, 
                fb_client=None
            )
            
            # Если шлюз не смог распознать или легализовать бумагу — прерываем обработку
            if not l_id:
                print(f"⚠️ [TG BOT]: Не удалось легализовать символ '{t_name}' через ядро СУБД.")
                return
            
            # Извлекаем созданные/найденные параметры
            listing_sql = f"""
                SELECT l.broker_symbol, t.symbol, t.company_name, l.currency_id, l.last_price
                FROM public.listings l
                JOIN public.tickers t ON l.ticker_id = t.id
                WHERE l.id = {l_id};
            """
            l_res = db_bot.execute_query(listing_sql)
            
        except Exception as err:
            print(f"❌ [КАСКАДНЫЙ ПОИСК ОШИБКА]: Инструмент {t_name} не найден на мировых биржах: {err}")
            await callback.answer(f"❌ Ошибка: тикер '{t_name}' не найден в СУБД и СУП брокера.", show_alert=True)
            return

    print(f"\n📈 [ТИКЕР ТРИГГЕР]: Рендеринг карточки `{pure_symbol}` (ID: {t_id}) | Портфель: {p_id} | Вкладка: {view}")
    await callback.answer(f"Анализ {pure_symbol}...")

    # Вытаскиваем знак валюты листинга
    sql_base_cur = f"SELECT sign FROM public.currencies WHERE id = '{currency_id}';"
    cur_row = db_bot.execute_row(sql_base_cur)
    sign = cur_row.get('sign', '$')

    # Единый header по стандарту UPort (см. Claude/05_strategy_screen_and_kubiki.md, BACKLOG.md #19) --
    # один на все варианты этого экрана, включая шторку алертов ниже
    header_text = await format_premium_header(t_id, p_id)

    # Динамически вычисляем типы отображения (Владение, Общий капитал, Исследование)
    is_owner_view = (p_id > 0 and p_id != 9999)
    is_family_view = (p_id == 0)
    is_research_portfolio = (p_id == 9999 and getattr(callback_data, 'listing_id', 0) is not None and int(getattr(callback_data, 'listing_id', 0)) > 0)

    # 2. СБОР СТРАТЕГИЧЕСКИХ ДАННЫХ И ХОЛДИНГ-ПЕРИОДОВ
    assets_info_sql = f"""
        SELECT a.portfolio_id, p.name AS portfolio_name, u.name AS owner_name, a.quantity, a.avg_price,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
        FROM public.assets a
        JOIN public.portfolios p ON a.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        WHERE a.listing_id = {l_id if l_id > 0 else -1} AND a.quantity > 0;
    """
    all_holders = db_bot.execute_query(assets_info_sql)
    if not isinstance(all_holders, list):
        all_holders = [all_holders] if all_holders else []

    # =========================================================================
    # 🔥 НОВЫЙ КОНТУР v1.0: ПРЯМОЙ ПЕРЕХВАТ И РЕНДЕРИНГ ШТОРКИ "СПИСОК АЛЕРТОВ"
    # =========================================================================
    if view == "alerts":
        print(f"🎯 [ТИКЕР АЛЕРТЫ]: Сборка изолированной шторки алертов для listing_id = {l_id}")
        await callback.answer("Загрузка радара алертов...")
        
        # Вытаскиваем все алерты тикера через наше новое реляционное ядро
        alerts_list = db_bot.get_ticker_alerts_context(l_id)

        # Единый header карточки UPort
        report_text = header_text
        report_text += f"🔔 **АЛЕРТЫ:**\n"

        if alerts_list:
            # Группировка по портфелю (Блок 5 стандарта body, см. Claude/05_...):
            # заголовок портфеля один раз, условия под ним -- по одной строке на алерт
            grouped, order_of_portfolios = {}, []
            for al in alerts_list:
                p_key = al['portfolio_name'] or f"П{al.get('portfolio_id') or p_id}"
                if p_key not in grouped:
                    grouped[p_key] = []
                    order_of_portfolios.append(p_key)
                grouped[p_key].append(al)

            for p_key in order_of_portfolios:
                report_text += f" • 💼 {p_key}\n"
                for al in grouped[p_key]:
                    report_text += format_alert_condition_line(al, last_price, currency_sign=sign)
        else:
            report_text += "   *Алертов нет.*\n"

        # Footer по стандарту (см. Claude/05_strategy_screen_and_kubiki.md): без явной фразы --
        # переход к тексту кнопок сразу очевиден, разделитель без фразы не нужен.
        # "Назад" зависит от того, откуда реально пришли (BACKLOG.md №31, alerts_origin выше)
        if alerts_origin == "from_ticker":
            nav_markup = generate_nav_back_keyboard(
                one_step_back_text="🔙 К карточке бумаги",
                full_back_callback=MenuAction(
                    action="view_ticker", portfolio_id=p_id, listing_id=l_id,
                    ticker_name=pure_symbol, sub_view="owner", strategy_id=strategy_id
                ).pack()
            )
        else:
            nav_markup = generate_nav_back_keyboard(
                one_step_back_text="🔙 Назад к радарам слежения",
                full_back_callback=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="assets").pack()
            )

        print("🖥️ [ТИКЕР АЛЕРТЫ]: Отправляю изолированную шторку в Telegram...")
        try:
            await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=nav_markup)
        except TelegramBadRequest:
            pass
        return # 🚨 ПРЕДОХРАНИТЕЛЬ: Выходим из метода, полностью блокируя выполнение старого кода кошелька!
    # =========================================================================

    # =========================================================================
    # 🔥 ШТОРКА "СПИСОК ОРДЕРОВ" (Блок 5 стандарта body, симметрично шторке алертов выше)
    # =========================================================================
    if view == "orders":
        print(f"🎯 [ТИКЕР ОРДЕРА]: Сборка изолированной шторки ордеров для ticker_id = {t_id}")
        await callback.answer("Загрузка активных приказов...")

        sql_live_orders = f"""
            SELECT u.name as owner_name, p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, cur.sign,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - o.created_at))::int AS order_age_days
            FROM public.orders o
            JOIN public.listings l ON o.listing_id = l.id
            JOIN public.portfolios p ON o.portfolio_id = p.id
            JOIN public.users u ON p.owner_id = u.id
            JOIN public.currencies cur ON l.currency_id = cur.id
            WHERE l.ticker_id = {t_id}
              AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
        """
        live_orders_res = db_bot.execute_query(sql_live_orders)
        if not isinstance(live_orders_res, list):
            live_orders_res = [live_orders_res] if live_orders_res else []

        report_text = header_text + "📃 **АКТИВНЫЕ ПРИКАЗЫ:**\n"

        # 4 категории (Блок 5 стандарта body, см. Claude/05_...): Покупка/Продажа --
        # обычные лимитные заявки, Stop Loss/Take Profit -- защитные триггеры.
        # Уточнено пользователем 2026-07-26: раньше делили только рыночные/триггерные,
        # теряя разницу купить/продать внутри каждого вида.
        category_order = ["buy", "sell", "stop_loss", "take_profit"]

        if is_owner_view:
            owner_row_shade = next((h for h in all_holders if h['portfolio_id'] == p_id), None)
            p_name_shade = owner_row_shade['portfolio_name'] if owner_row_shade else f"П{p_id}"
            current_orders = [o for o in live_orders_res if o['portfolio_name'] == p_name_shade]

            if current_orders:
                blocks = []
                for cat_key in category_order:
                    icon, label, _ = ORDER_CATEGORIES[cat_key]
                    cat_orders = [o for o in current_orders if classify_order(o) == cat_key]
                    if cat_orders:
                        block = f"{icon} **{label}:**\n"
                        for o in cat_orders:
                            block += format_order_line(o, last_price)
                        blocks.append(block)
                report_text += "\n".join(blocks)
            else:
                report_text += "   *Приказов нет.*\n"
        else:
            if live_orders_res:
                distinct_owners = sorted(set(o['owner_name'] for o in live_orders_res))
                owner_blocks = []
                for owner in distinct_owners:
                    owner_orders = [o for o in live_orders_res if o['owner_name'] == owner]
                    p_title_str = owner_orders[0]['portfolio_name'] if owner_orders else ""
                    owner_block = f"👤 **{owner} ({p_title_str}):**\n"
                    cat_blocks = []
                    for cat_key in category_order:
                        icon, label, _ = ORDER_CATEGORIES[cat_key]
                        cat_orders = [o for o in owner_orders if classify_order(o) == cat_key]
                        if cat_orders:
                            cat_block = f" {icon} {label}:\n"
                            for o in cat_orders:
                                cat_block += format_order_line(o, last_price)
                            cat_blocks.append(cat_block)
                    owner_block += "\n".join(cat_blocks)
                    owner_blocks.append(owner_block)
                report_text += "\n".join(owner_blocks)
            else:
                report_text += "   *Приказов нет.*\n"

        nav_markup = generate_nav_back_keyboard(
            one_step_back_text="🔙 К карточке бумаги",
            full_back_callback=MenuAction(
                action="view_ticker", portfolio_id=p_id, listing_id=l_id,
                ticker_name=pure_symbol, sub_view="owner", strategy_id=strategy_id
            ).pack()
        )

        print("🖥️ [ТИКЕР ОРДЕРА]: Отправляю изолированную шторку в Telegram...")
        try:
            await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=nav_markup)
        except TelegramBadRequest:
            pass
        return
    # =========================================================================

    # 3. ФОРМИРОВАНИЕ УЛЬТИМАТИВНЫХ ШАПОК СТУДИИ ДИЗАЙНА UPORT
    if is_owner_view:
        # Вариант 1: В портфеле конкретного счета -- Блок 1 стандарта body
        # (см. Claude/05_strategy_screen_and_kubiki.md): финансовая характеристика,
        # несёт собственную подпись портфеля/стратегии вместо убранной строки-контекста.
        # p_name нужен ниже (раздел ордеров) для фильтрации по названию портфеля.
        owner_row = next((h for h in all_holders if h['portfolio_id'] == p_id), None)
        p_name = owner_row['portfolio_name'] if owner_row else f"П{p_id}"

        text = header_text
        text += await format_position_financials(
            portfolio_id=p_id, listing_id=l_id, last_price=last_price, sign=sign, strategy_id=strategy_id
        )
        # Блок 6 стандарта body: где ещё в семье есть/запланировано, за вычетом
        # текущего портфеля/стратегии (уже показан Блоком 1 выше)
        text += f"{SEPARATOR_LINE}\n"
        text += await format_cross_holdings(t_id, l_id, portfolio_id=p_id, strategy_id=strategy_id)

    elif is_family_view:
        # Вариант 2: В Сводном семейном капитале -- целиком Блок 6 (портфельный разрез,
        # без исключения -- показываем всю семью)
        text = header_text
        text += await format_cross_holdings(t_id, l_id, portfolio_id=0, strategy_id=0)

    elif is_research_portfolio:
        # Вариант 4: Исследование В ПОРТФЕЛЕ (Фокус конкретной стратегии) -- вне темы,
        # завязано на устаревший портфель 9999 (см. Claude/BACKLOG.md №11), не трогаем
        text = header_text
        text += f"ИССЛЕДОВАНИЕ ПО СТРАТЕГИИ\n"
        text += f"🎯 Статус: Наблюдение по декларации счета\n"
    else:
        # Вариант 3: Исследование ВООБЩЕ (Глобальная песочница 9999) -- Блок 6
        # (портфельный разрез, 9999 не держит реальных активов -- исключение инертно)
        text = header_text
        text += await format_cross_holdings(t_id, l_id, portfolio_id=p_id, strategy_id=0)

    # Блок 2 ("Поведение") и Блок 4 ("Рыночные сигналы") стандарта body (см.
    # Claude/05_strategy_screen_and_kubiki.md) -- оба свойство самого тикера, общее
    # для всех 4 контекстов владения выше, поэтому один общий разделитель на пару
    text += f"{SEPARATOR_LINE}\n"
    text += await format_ticker_behavior(t_id, listing_id=l_id, portfolio_id=p_id)
    text += await format_market_signals(t_id)

    # 4. ВЫЗОВ РИСК-АУДИТОРА IPS (Только для личного портфеля или портфельного фокуса)
    if is_owner_view or is_research_portfolio:
        audit_id = p_id if is_owner_view else 1
        # 🔥 РЕФАКТОРИНГ: Заменяем на чистый execute_row
        p_row = await asyncio.to_thread(db_bot.execute_row, f"SELECT name FROM public.portfolios WHERE id = {audit_id};")
        p_title = p_row.get('name', f"Счет #{audit_id}")

        warnings_list = audit_ticker_for_portfolio(broker_symbol if l_id > 0 else pure_symbol, audit_id, db_bot)
        text += f"{SEPARATOR_LINE}\n🛡️ **Риск-аудит IPS {p_title}:**\n"
        if warnings_list:
            for w in warnings_list:
                text += f" {w}\n"
        else:
            text += " ✅ Риск-декларация: лимиты веса и налоговой нагрузки соблюдены.\n"

    text += f"{SEPARATOR_LINE}\n"

    # Вкладка "Метрики Yahoo" убрана (Блок 4 стандарта body заменил её содержимое --
    # см. format_market_signals выше; фундаментал в старом виде больше не нужен как
    # отдельный экран). Единственная оставшаяся вкладка была бы всегда одна и та же
    # (current_sub_view всегда совпадает) -- переключатель вкладок (footer, ряд 2)
    # больше не нужен вообще для этого экрана.
    builder = InlineKeyboardBuilder()

    # Блок 5 стандарта body (см. Claude/05_strategy_screen_and_kubiki.md): компактная
    # сводка алертов/ордеров + кнопки "подробнее" в отдельные шторки (sub_view=alerts/orders).
    sql_live_orders = f"""
        SELECT u.name as owner_name, p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, cur.sign,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - o.created_at))::int AS order_age_days
        FROM public.orders o
        JOIN public.listings l ON o.listing_id = l.id
        JOIN public.portfolios p ON o.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        JOIN public.currencies cur ON l.currency_id = cur.id
        WHERE l.ticker_id = {t_id}
          AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
    """
    live_orders_res = db_bot.execute_query(sql_live_orders)
    if not isinstance(live_orders_res, list):
        live_orders_res = [live_orders_res] if live_orders_res else []

    if is_owner_view:
        orders_count = len([o for o in live_orders_res if o['portfolio_name'] == p_name])
    else:
        orders_count = len(live_orders_res)

    alerts_list = db_bot.get_ticker_alerts_context(l_id)
    alerts_count = len([al for al in alerts_list if not (p_id != 0 and al.get('portfolio_id') != p_id)])

    text += format_broker_link_summary(alerts_count, orders_count)

    builder.row(
        types.InlineKeyboardButton(
            text=f"🔔 Алерты ({alerts_count})",
            callback_data=MenuAction(
                action="view_ticker", portfolio_id=p_id, listing_id=l_id,
                ticker_name=pure_symbol, sub_view="alerts/from_ticker", strategy_id=strategy_id
            ).pack()
        ),
        types.InlineKeyboardButton(
            text=f"📃 Приказы ({orders_count})",
            callback_data=MenuAction(
                action="view_ticker", portfolio_id=p_id, listing_id=l_id,
                ticker_name=pure_symbol, sub_view="orders", strategy_id=strategy_id
            ).pack()
        )
    )

    if is_owner_view:
        builder.row(types.InlineKeyboardButton(
            text="🔗 Привязать ордер к плану",
            callback_data=MenuAction(action="pipeline_link_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
        ))

    # Footer, ряд 4 -- навигация (см. Claude/05_strategy_screen_and_kubiki.md): один общий кубик
    # вместо ручной сборки "назад" + "главное меню" по отдельности
    if strategy_id > 0:
        back_text = "🔙 К стратегии"
        back_callback = MenuAction(action="view_strategy", strategy_id=strategy_id, portfolio_id=p_id).pack()
    elif is_owner_view:
        back_text = "🔙 К списку активов"
        back_callback = MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()
    elif is_family_view:
        back_text = "🔙 К сводке капитала"
        back_callback = MenuAction(action="view_portfolio", portfolio_id=0, sub_view="assets").pack()
    else:
        back_text = "🔙 В главное меню"
        back_callback = MenuAction(action="main_menu").pack()

    nav_markup = generate_nav_back_keyboard(one_step_back_text=back_text, full_back_callback=back_callback)
    builder.attach(InlineKeyboardBuilder.from_markup(nav_markup))

    print("🖥️ [ТИКЕР]: Отправляю карточку акции в Telegram...")

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest as e:
        print(f"⚠️ Ошибка редактирования шторки тикера: {e}")
        pass


@router.callback_query(MenuAction.filter(F.action == "stop_price_alert"))
async def process_stop_price_alert(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Кнопка "🛑 Остановить" у уведомления PriceMoveWatcher (см. Claude/05_...): помечает
    алерт неактивным -- вотчер больше не будет повторять уведомление по нему (см.
    analytics/price_move_watcher.py, поле periodic).
    """
    alert_id = callback_data.alert_id
    db_sys.execute_query(
        f"UPDATE public.alerts SET is_active = false, updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) WHERE id = {int(alert_id)};"
    )
    await callback.answer("Остановлено.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
