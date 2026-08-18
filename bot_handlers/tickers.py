import asyncio
import logging
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
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
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_ticker_footer_keyboard
from bot_handlers.watchlist import get_watchlist_removal_status

# Импортируем наш аналитический модуль аудита лимитов под стратегию (IPS)
from analytics.portfolio_auditor import audit_ticker_for_portfolio
# Мультивалютность (BACKLOG.md #30) -- плоская FX-конвертация для сумм брокерского слоя
from analytics.analytics_utils import convert_currency_amount
from analytics.order_alert_staleness import is_broker_order_price_stale

# Инициализируем локальный роутер для модуля карточек акций
router = Router()


@router.callback_query(MenuAction.filter(F.action == "view_ticker"))
async def process_view_ticker(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Экран Уровня 3: Детализация конкретной бумаги с глубоким дебагом."""
    
    logging.debug(f"📥 [ТИКЕР]: callback_data: {callback_data}")

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

    logging.debug(f"📥 [ТИКЕР]: l_id={l_id} | ticker_name='{t_name}' | view='{view}'")

    # 1. РЕЛЯЦИОННОЕ ОПРЕДЕЛЕНИЕ ТИКЕРА И ПЛОЩАДКИ БРОКЕРА
    if l_id > 0:
        # Сценарий А: Вход из инлайн-кнопок портфеля по числовому ID листинга
        listing_sql = """
            SELECT l.broker_symbol, l.ticker_id, t.symbol, t.company_name, l.currency_id, l.last_price
            FROM public.listings l
            JOIN public.tickers t ON l.ticker_id = t.id
            WHERE l.id = %s;
        """
        # 🔥 РЕФАКТОРИНГ: Заменяем на execute_row, убирая кашу проверок на списки/словари
        l_row = db_bot.execute_row(listing_sql, (l_id,))

        if not l_row:
            logging.warning(f"⚠️ [ТИКЕР]: СУБД не нашла листинг l_id={l_id} (дохлая ссылка в кнопке).")
            await callback.answer("❌ Листинг актива не найден в СУБД.", show_alert=True)
            return

        t_id = int(l_row['ticker_id'])
        pure_symbol = l_row['symbol']
        broker_symbol = l_row['broker_symbol']
        last_price = float(l_row['last_price'] or 0)
        currency_id = l_row['currency_id']
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
                logging.warning(f"⚠️ [ТИКЕР]: Не удалось легализовать символ '{t_name}' через ядро СУБД.")
                return
            
            # Извлекаем созданные/найденные параметры
            listing_sql = """
                SELECT l.broker_symbol, t.symbol, t.company_name, l.currency_id, l.last_price
                FROM public.listings l
                JOIN public.tickers t ON l.ticker_id = t.id
                WHERE l.id = %s;
            """
            l_res = db_bot.execute_query(listing_sql, (l_id,))
            
        except Exception as err:
            logging.error(f"❌ [ТИКЕР]: Инструмент {t_name} не найден на мировых биржах: {err}")
            await callback.answer(f"❌ Ошибка: тикер '{t_name}' не найден в СУБД и СУП брокера.", show_alert=True)
            return

    await callback.answer(f"Анализ {pure_symbol}...")

    # Мультивалютность (BACKLOG.md #30): вся денежная информация выводится в
    # base_currency СМОТРЯЩЕГО пользователя, не в валюте листинга/брокера. user_db_id
    # уже определён при /start (см. bot_handlers/summary.py::cmd_start) и лежит в FSM --
    # здесь просто читаем, не определяем личность заново. Курс считаем ОДИН РАЗ за
    # рендер и передаём дальше во все блоки, вместо того чтобы каждый блок лез в БД сам.
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    target_currency = "USD"
    if user_db_id:
        user_row = await asyncio.to_thread(db_bot.execute_row, "SELECT base_currency FROM public.users WHERE id = %s;", (user_db_id,))
        if user_row and user_row.get("base_currency"):
            target_currency = user_row["base_currency"]

    target_cur_row = await asyncio.to_thread(db_bot.execute_row, "SELECT sign FROM public.currencies WHERE id = %s;", (target_currency,))
    target_sign = target_cur_row.get('sign', '$') if target_cur_row else '$'
    fx_rate = await asyncio.to_thread(convert_currency_amount, db_bot, 1.0, currency_id, target_currency)
    last_price_display = last_price * fx_rate

    # Единый header по стандарту UPort (см. Claude/05_strategy_screen_and_kubiki.md, BACKLOG.md #19) --
    # один на все варианты этого экрана, включая шторку алертов ниже
    header_text = await format_premium_header(t_id, p_id, target_currency=target_currency, target_sign=target_sign)

    # Динамически вычисляем типы отображения (Владение, Общий капитал). Портфель 9999
    # ("Семейная лаборатория ИИ") удалён (см. Claude/BACKLOG.md, Трек C, п.11) -- эти два
    # случая исчерпывающие, третьей ветки ("исследование в 9999") больше нет.
    is_owner_view = (p_id > 0)
    is_family_view = (p_id == 0)

    # 2. СБОР СТРАТЕГИЧЕСКИХ ДАННЫХ И ХОЛДИНГ-ПЕРИОДОВ
    assets_info_sql = """
        SELECT a.portfolio_id, p.name AS portfolio_name, u.name AS owner_name, a.quantity, a.avg_price,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
        FROM public.assets a
        JOIN public.portfolios p ON a.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        WHERE a.listing_id = %s AND a.quantity > 0;
    """
    all_holders = db_bot.execute_query(assets_info_sql, (l_id if l_id > 0 else -1,))
    if not isinstance(all_holders, list):
        all_holders = [all_holders] if all_holders else []

    # =========================================================================
    # 🔥 НОВЫЙ КОНТУР v1.0: ПРЯМОЙ ПЕРЕХВАТ И РЕНДЕРИНГ ШТОРКИ "СПИСОК АЛЕРТОВ"
    # =========================================================================
    if view == "alerts":
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
                    report_text += format_alert_condition_line(al, last_price_display, currency_sign=target_sign, fx_rate=fx_rate)
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
        await callback.answer("Загрузка активных приказов...")

        sql_live_orders = """
            SELECT u.name as owner_name, p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, cur.sign,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - o.created_at))::int AS order_age_days
            FROM public.orders o
            JOIN public.listings l ON o.listing_id = l.id
            JOIN public.portfolios p ON o.portfolio_id = p.id
            JOIN public.users u ON p.owner_id = u.id
            JOIN public.currencies cur ON l.currency_id = cur.id
            WHERE l.ticker_id = %s
              AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
        """
        live_orders_res = db_bot.execute_query(sql_live_orders, (t_id,))
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
                            block += format_order_line(o, last_price_display, fx_rate=fx_rate, target_sign=target_sign)
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
                                cat_block += format_order_line(o, last_price_display, fx_rate=fx_rate, target_sign=target_sign)
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

        try:
            await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=nav_markup)
        except TelegramBadRequest:
            pass
        return
    # =========================================================================

    # =========================================================================
    # 🔥 ШТОРКА "ПЛАН" (v0, см. Claude/11_asset_lifecycle_and_plan.md) -- показывает
    # уже существующий order_pipelines (PENDING/ACTIVE) как читаемую шпаргалку.
    # Динамические SL/TP и календарный чек-пойнт ещё не реализованы -- показываем
    # только то, что реально уже есть в данных.
    # =========================================================================
    if view == "plan":
        await callback.answer("Загрузка плана...")

        # Единая логика для реального И бумажного портфеля (Claude/BACKLOG.md
        # №122/123) -- execution_mode больше не ветвит этот экран вообще, только то,
        # что происходит ПОСЛЕ создания Плана (человек у брокера vs paper_broker.py).
        plan_rows = db_bot.execute_query("""
            SELECT op.id, op.strategy_id, s.strategy_name, s.rules_config,
                   op.current_step, op.target_quantity, op.initial_entry_price,
                   op.pending_broker_order_id, op.step_ready_notified_at, op.entry_trigger_override,
                   sa.allocated_quantity,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - a.position_opened_at))::int AS days_held,
                   (SELECT COUNT(*) FROM public.strategy_tactics st WHERE st.strategy_id = op.strategy_id) AS total_steps,
                   (SELECT trigger_conditions FROM public.strategy_tactics st
                        WHERE st.strategy_id = op.strategy_id AND st.step_number = op.current_step) AS current_step_conditions
            FROM public.order_pipelines op
            JOIN public.strategies s ON s.id = op.strategy_id
            LEFT JOIN public.assets a ON a.portfolio_id = op.portfolio_id AND a.listing_id = op.listing_id
            LEFT JOIN public.strategy_assets sa ON sa.asset_id = a.id AND sa.strategy_id = op.strategy_id
            WHERE op.portfolio_id = %s AND op.listing_id = %s
              AND op.pipeline_status IN ('PENDING', 'ACTIVE');
        """, (p_id, l_id))
        plan_rows = plan_rows if isinstance(plan_rows, list) else ([plan_rows] if plan_rows else [])

        report_text = header_text + "📋 **ПЛАН:**\n"

        if not plan_rows:
            report_text += "   *Активного плана по этой бумаге сейчас нет.*\n"
        else:
            for plan in plan_rows:
                rules = plan.get("rules_config") or {}
                total_steps = int(plan.get("total_steps") or 1)
                current_step = int(plan["current_step"])
                target_qty = float(plan["target_quantity"] or 0)
                bought_qty = float(plan.get("allocated_quantity") or 0)
                entry_price = float(plan.get("initial_entry_price") or 0)

                report_text += f"\n🎯 *{plan['strategy_name']}* — шаг {current_step} из {total_steps}\n"
                report_text += f"└ Куплено: {bought_qty:.0f} из {target_qty:.0f} шт"
                if entry_price > 0:
                    report_text += f" по ${entry_price:.2f}"
                report_text += "\n"

                time_limit = rules.get("tactic_time_limit_days")
                days_held = plan.get("days_held")
                if days_held is not None:
                    if time_limit:
                        report_text += f"└ Дней в позиции: {days_held} из {int(time_limit)}\n"
                    else:
                        report_text += f"└ Дней в позиции: {days_held}\n"

                target_profit = rules.get("tactic_target_profit_pct")
                if target_profit:
                    report_text += f"└ Цель по доходности: +{float(target_profit):.1f}%\n"

                if current_step > total_steps:
                    report_text += "└ Все ступени лесенки уже пройдены.\n"
                else:
                    # Шаг 1 с entry_trigger_override (шпаргалка «К сделке», Claude/BACKLOG.md
                    # №122/123) -- условие зафиксировано на самом Плане, не в strategy_tactics
                    # (у «Неопределённой», например, тактик нет вообще).
                    override = plan.get("entry_trigger_override") or {}
                    conditions = override if (current_step == 1 and override) else (plan.get("current_step_conditions") or {})
                    mode = conditions.get("mode")
                    if mode == "market":
                        report_text += "└ Условие следующего шага: рыночная заявка, можно ставить сразу.\n"
                    elif mode == "limit_fixed" and conditions.get("price"):
                        report_text += f"└ Условие следующего шага: цена ${float(conditions['price']):,.2f}.\n"
                    elif mode == "limit":
                        cond_parts = []
                        if conditions.get("max_rsi") is not None:
                            cond_parts.append(f"RSI ≤ {conditions['max_rsi']}")
                        if conditions.get("price_drop_pct") is not None:
                            cond_parts.append(f"просадка ≥ {conditions['price_drop_pct']}% от входа")
                        report_text += f"└ Условие следующего шага: {', '.join(cond_parts) if cond_parts else 'лимитная заявка'}.\n"
                    else:
                        report_text += "└ Условие следующего шага ещё не задано в тактике стратегии.\n"

                if plan.get("pending_broker_order_id"):
                    report_text += f"└ Ожидается исполнение приказа №{plan['pending_broker_order_id']}.\n"
                    # Живая проверка (не хранимый флаг) -- см. analytics/order_alert_staleness.py,
                    # заменил analytics/stale_order_watcher.py (2026-07-30, п.5 БЭКЛОГА).
                    if is_broker_order_price_stale(db_bot, plan["pending_broker_order_id"]):
                        report_text += "└ ⚠️ Привязанный приказ мог устареть — цена ушла от цены приказа.\n"
                else:
                    report_text += "└ Приказ на текущий шаг ещё не привязан — используйте «🔗 Привязать ордер к плану».\n"

                if plan.get("step_ready_notified_at"):
                    report_text += "└ 🪜 Условие следующего шага уже выполнено — пора ставить приказ.\n"

        # Кнопки-действия ВНУТРИ вкладки Плана (согласовано 2026-07-29, см.
        # Claude/11_asset_lifecycle_and_plan.md) -- раньше жили на основной карточке,
        # теперь единая точка входа "📋 План" ведёт сюда, а отсюда уже разветвляется.
        action_builder = InlineKeyboardBuilder()

        # Кнопка видна, только если реально есть что привязывать -- та же проверка,
        # что и в самом pipeline_link_start (order_pipelines.py), по просьбе
        # пользователя 2026-07-29 (раньше показывалась всегда, даже без единого приказа).
        # Для бумажного портфеля это условие практически никогда не выполняется --
        # paper_broker.py исполняет через distribute_asset_delta напрямую, минуя
        # "висящий непривязанный приказ" целиком (Claude/BACKLOG.md №117, Вопрос 5).
        unlinked_order = db_bot.execute_row("""
            SELECT 1 FROM public.orders o
            WHERE o.portfolio_id = %s AND o.ticker_id = %s
              AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED')
              AND NOT EXISTS (
                  SELECT 1 FROM public.order_pipelines op
                  WHERE op.pending_broker_order_id = o.broker_order_id
              )
            LIMIT 1;
        """, (p_id, t_id))
        if unlinked_order:
            action_builder.row(types.InlineKeyboardButton(
                text="🔗 Привязать ордер к плану",
                callback_data=MenuAction(action="pipeline_link_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
            ))

        # «🤝 К сделке» (Claude/BACKLOG.md №122/123) -- заменяет «📝 План входа», единая
        # кнопка для реального И бумажного портфеля. strategy_id уже известен из контекста
        # (пришли с карточки стратегии) -- сразу в шпаргалку; неизвестен -- через пикер
        # стратегии (plan_from_idea_start, сохранён -- проверяет "держится в другой
        # стратегии" и предлагает перенос вместо дублирующей покупки).
        if not plan_rows:
            if strategy_id > 0:
                action_builder.row(types.InlineKeyboardButton(
                    text="🤝 К покупке",
                    callback_data=MenuAction(action="deal_start_buy", portfolio_id=p_id, strategy_id=strategy_id, ticker_id=t_id).pack()
                ))
            else:
                action_builder.row(types.InlineKeyboardButton(
                    text="🤝 К покупке",
                    callback_data=MenuAction(action="plan_from_idea_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
                ))

        # Симметрично для продажи -- видна только если бумага реально держится хоть в
        # одной стратегии. Если держится в НЕСКОЛЬКИХ -- ведёт через plan_exit_start
        # (пикер стратегии-источника, сохранён), иначе сразу в шпаргалку.
        holder_rows = db_bot.execute_query("""
            SELECT sa.strategy_id FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s AND sa.allocated_quantity > 0;
        """, (p_id, l_id))
        holder_rows = holder_rows if isinstance(holder_rows, list) else ([holder_rows] if holder_rows else [])
        if holder_rows:
            if len(holder_rows) == 1:
                action_builder.row(types.InlineKeyboardButton(
                    text="🤝 К продаже",
                    callback_data=MenuAction(
                        action="deal_start_sell", portfolio_id=p_id, strategy_id=int(holder_rows[0]["strategy_id"]), listing_id=l_id
                    ).pack()
                ))
            else:
                action_builder.row(types.InlineKeyboardButton(
                    text="🤝 К продаже",
                    callback_data=MenuAction(action="plan_exit_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
                ))
            # "Перенос" -- единый механизм переноса между стратегиями (согласовано
            # 2026-07-30, см. Claude/11_asset_lifecycle_and_plan.md, "реинкарнация").
            action_builder.row(types.InlineKeyboardButton(
                text="🔄 Перенести в другую стратегию",
                callback_data=MenuAction(action="transfer_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
            ))

        nav_kb = generate_nav_back_keyboard(
            one_step_back_text="🔙 К карточке бумаги",
            full_back_callback=MenuAction(
                action="view_ticker", portfolio_id=p_id, listing_id=l_id,
                ticker_name=pure_symbol, sub_view="owner", strategy_id=strategy_id
            ).pack()
        )
        final_builder = InlineKeyboardBuilder.from_markup(action_builder.as_markup())
        final_builder.attach(InlineKeyboardBuilder.from_markup(nav_kb))
        nav_markup = final_builder.as_markup()

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
        # несёт собственную подпись портфеля + таблицу-разбивку по стратегиям вместо
        # убранной строки-контекста. p_name нужен ниже (раздел ордеров) для фильтрации
        # по названию портфеля.
        owner_row = next((h for h in all_holders if h['portfolio_id'] == p_id), None)
        p_name = owner_row['portfolio_name'] if owner_row else f"П{p_id}"

        text = header_text
        text += await format_position_financials(
            portfolio_id=p_id, listing_id=l_id, last_price=last_price_display, sign=target_sign, fx_rate=fx_rate
        )
        # Блок 6 стандарта body: где ещё в семье есть/запланировано, за вычетом
        # текущего портфеля/стратегии (уже показан Блоком 1 выше)
        text += f"{SEPARATOR_LINE}\n"
        text += await format_cross_holdings(t_id, l_id, portfolio_id=p_id, strategy_id=strategy_id)

    else:
        # Вариант 2: В Сводном семейном капитале -- целиком Блок 6 (портфельный разрез,
        # без исключения -- показываем всю семью)
        text = header_text
        text += await format_cross_holdings(t_id, l_id, portfolio_id=0, strategy_id=0)

    # Блок 2 ("Поведение") и Блок 4 ("Рыночные сигналы") стандарта body (см.
    # Claude/05_strategy_screen_and_kubiki.md) -- оба свойство самого тикера, общее
    # для всех 4 контекстов владения выше, поэтому один общий разделитель на пару
    text += f"{SEPARATOR_LINE}\n"
    text += await format_ticker_behavior(t_id, listing_id=l_id, portfolio_id=p_id)
    text += await format_market_signals(t_id)

    # 4. ВЫЗОВ РИСК-АУДИТОРА IPS (Только для личного портфеля)
    if is_owner_view:
        audit_id = p_id
        # 🔥 РЕФАКТОРИНГ: Заменяем на чистый execute_row
        p_row = await asyncio.to_thread(db_bot.execute_row, "SELECT name FROM public.portfolios WHERE id = %s;", (audit_id,))
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

    # Блок 5 стандарта body (см. Claude/05_strategy_screen_and_kubiki.md): компактная
    # сводка алертов/ордеров + кнопки "подробнее" в отдельные шторки (sub_view=alerts/orders).
    sql_live_orders = """
        SELECT u.name as owner_name, p.name as portfolio_name, o.oper, o.type, o.q, o.p, o.stop_price, cur.sign,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - o.created_at))::int AS order_age_days
        FROM public.orders o
        JOIN public.listings l ON o.listing_id = l.id
        JOIN public.portfolios p ON o.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        JOIN public.currencies cur ON l.currency_id = cur.id
        WHERE l.ticker_id = %s
          AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED');
    """
    live_orders_res = db_bot.execute_query(sql_live_orders, (t_id,))
    if not isinstance(live_orders_res, list):
        live_orders_res = [live_orders_res] if live_orders_res else []

    if is_owner_view:
        orders_count = len([o for o in live_orders_res if o['portfolio_name'] == p_name])
    else:
        orders_count = len(live_orders_res)

    alerts_list = db_bot.get_ticker_alerts_context(l_id)
    alerts_count = len([al for al in alerts_list if not (p_id != 0 and al.get('portfolio_id') != p_id)])

    text += format_broker_link_summary(alerts_count, orders_count)

    # Кнопка «Убрать из СН» (см. Claude/BACKLOG.md, обсуждение 2026-07-30) -- только
    # владельческий контекст (portfolio_id известен) и только если реально безопасно.
    watchlist_removable = False
    if is_owner_view:
        wl_status = await asyncio.to_thread(get_watchlist_removal_status, db_bot, p_id, l_id)
        watchlist_removable = bool(wl_status and wl_status["is_safe"])

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

    # Блок 5 (Алерты/Приказы/План) + навигация -- общий кубик (см. Claude/BACKLOG.md,
    # выделен 2026-07-29 вместе со стандартизацией «Предложений»).
    reply_markup = generate_ticker_footer_keyboard(
        portfolio_id=p_id, listing_id=l_id, symbol=pure_symbol, strategy_id=strategy_id,
        is_owner_view=is_owner_view, alerts_count=alerts_count, orders_count=orders_count,
        back_text=back_text, back_callback=back_callback, watchlist_removable=watchlist_removable
    )

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logging.warning(f"⚠️ [ТИКЕР]: Ошибка редактирования шторки тикера: {e}")


@router.callback_query(MenuAction.filter(F.action == "stop_price_alert"))
async def process_stop_price_alert(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Кнопка "🛑 Остановить" у уведомления PriceMoveWatcher (см. Claude/05_...): помечает
    алерт неактивным -- вотчер больше не будет повторять уведомление по нему (см.
    analytics/price_move_watcher.py, поле periodic).
    """
    alert_id = callback_data.alert_id
    db_sys.execute_query(
        "UPDATE public.alerts SET is_active = false, updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) WHERE id = %s;",
        (alert_id,)
    )
    await callback.answer("Остановлено.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
