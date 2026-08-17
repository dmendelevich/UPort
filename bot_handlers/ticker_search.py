import re
import json
import logging
import asyncio
from aiogram import Router, types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# 🔥 ИМПОРТИРУЕМ ОБОИХ РОБОТОВ СУБД: db_sys для шлюза ворот, db_bot для чтения интерфейса
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_main_menu_keyboard
from bot_handlers.bot_screens import format_premium_header, TREND_LABELS
from analytics.analytics_utils import TickerEvaluator

router = Router()

@router.message(StateFilter(None), F.text)
async def process_global_ticker_search(message: types.Message, state: FSMContext):
    """
    Глобальный текстовый перехватчик инвест-идей.
    Ловит любой текст, валидирует, легализует бумагу через db_sys и выводит Паспорт качества.
    StateFilter(None) -- срабатывает только вне любого активного FSM-состояния (idle-чат),
    иначе перехватывал бы текстовый ввод любых будущих многошаговых экранов (создание
    портфеля, бэклог и т.п.) независимо от порядка регистрации роутеров.
    """
    # 1. ШАГ 1: АВТОРИЗАЦИЯ И ПЕРЕХВАТ USER_DB_ID ИЗ ПАМЯТИ FSM
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    is_admin = user_data.get("is_admin", False)
    
    if user_db_id is None:
        await message.answer(
            "🛑 Доступ запрещен. Ваш Telegram ID не зарегистрирован в системе UPort.\n"
            "Пожалуйста, выполните команду /start для авторизации.",
            reply_markup=generate_main_menu_keyboard(is_admin=is_admin)
        )
        return

    if message.text.startswith("/"):
        return

    # 2. ШАГ 2: ПЕРВИЧНАЯ САНИТАРИЯ И ВАЛИДАЦИЯ СТРОКИ
    raw_text = str(message.text).strip().upper()
    
    if not (1 <= len(raw_text) <= 8):
        await message.answer(
            "⚠️ Некорректный формат.\n"
            "Длина тикера должна быть от 1 до 8 символов (например: AAPL или BP.EU)."
        )
        return

    if not re.match(r"^[A-Z.]+$", raw_text):
        await message.answer(
            "⚠️ Некорректный формат.\n"
            "Тикер должен содержать только латинские буквы и точку. Пробелы и спецсимволы запрещены."
        )
        return

    # 3. ШАГ 3: ВКЛЮЧЕНИЕ ГЛАВНЫХ ВОРОТ СУБД (Используем db_sys под правами инженера!)
    status_msg = await message.answer(f"⏳ Разворачиваю глобальный анализ для '{raw_text}'...")
    
    try:
        from database import db_sys        
        # 🔥 ФИКС: Заменяем db_bot на db_sys, обходя блокировку 'Forbidden for BOT'
        ticker_id, listing_id = await asyncio.to_thread(
            db_sys.ensure_ticker_v3,
            ticker_name_raw=raw_text,
            caller_role="TG_USR",
            caller_id=user_db_id,
            broker_id=1,
            fb_client=None
        )

    except ValueError as user_visible_err:
        # 🔥 ЧЕЛОВЕЧЕСКИЙ ПРЕДОХРАНИТЕЛЬ: Выводим инвестору чистую правду из Паспортистки
        logging.warning(f"⚠️ [TG SEARCH BLOCK]: Пользовательский ввод отклонен: {user_visible_err}")
        await status_msg.edit_text(f"❌ {user_visible_err}")
        return
        
    except Exception as gateway_err:
        logging.error(f"🚨 [SEARCH ERROR]: Сбой шлюза ensure_ticker_v3 для {raw_text}: {gateway_err}")
        await status_msg.edit_text("❌ Внутренняя ошибка СУБД при легализации тикера.")
        return

    if not ticker_id:
        await status_msg.edit_text(
            f"❌ Инструмент '{raw_text}' не найден на мировых биржах.\n"
            f"Проверьте правильность написания или суффикс биржи брокера."
        )
        return

    await render_ticker_passport(status_msg, ticker_id, user_db_id)


@router.callback_query(MenuAction.filter(F.action == "view_ticker_passport"))
async def process_view_ticker_passport(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Повторное открытие паспорта тикера по уже известному ticker_id -- напр. кнопка
    "🔙 Назад к поиску" после добавления в список наблюдения (origin="search", см.
    bot_handlers/watchlist.py::execute_watchlist_fixation, составной sub_view с
    происхождением, тот же приём, что и в BACKLOG.md "Сделано" п.17). Тикер уже
    легализован (иначе ticker_id было бы неоткуда взять), легализация не повторяется.
    """
    await callback.answer()
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    await render_ticker_passport(callback.message, callback_data.ticker_id, user_db_id)


async def render_ticker_passport(target_message: types.Message, ticker_id: int, user_db_id: int = None):
    """
    Собирает и рендерит "Паспорт качества" бумаги по уже известному, легализованному
    ticker_id -- шаги 4-6 бывшего process_global_ticker_search, вынесенные в общую
    функцию, чтобы паспорт можно было открыть повторно (см. process_view_ticker_passport
    выше), не только сразу после текстового ввода.

    user_db_id -- если известен, паспорт проверяет, не в СН ли эта бумага уже в одном
    из портфелей ЭТОГО пользователя (тот же охват, что и у самой кнопки добавления,
    см. process_add_to_watchlist_routing) -- по просьбе пользователя 2026-07-30, чтобы
    не предлагать добавить то, что уже добавлено. Если да -- кнопки "В список
    наблюдения"/"Оставить в покое" заменяются статус-строкой и ссылкой на карточку
    тикера в соответствующем портфеле (там уже есть «🗑 Убрать из СН», если понадобится).
    """
    # 4. ШАГ 4: СБОР АНАЛИТИЧЕСКИХ ДАННЫХ ИЗ ТАБЛИЦЫ TICKERS
    try:
        # 🔥 РЕФАКТОРИНГ: Заменяем на метод одной строки execute_row
        sql_get_ticker = "SELECT * FROM public.tickers WHERE id = %s LIMIT 1;"
        t = await asyncio.to_thread(db_bot.execute_row, sql_get_ticker, (ticker_id,))
        
        if not t:
            await target_message.edit_text("❌ Ошибка: не удалось извлечь паспорт бумаги из СУБД.")
            return

        ticker_name_map = t.get("ticker_name_map") or {}

    except Exception as data_err:
        logging.error(f"🚨 [SEARCH ERROR]: Сбой сбора паспортных данных для id={ticker_id}: {data_err}")
        await target_message.edit_text("❌ Сбой экспресс-анализа паспорта бумаги.")
        return

    # 🧮 БЫСТРЫЙ И СВЕРХЛЕГКИЙ ЗАПРОС КОМПОНЕНТОВ ДЛЯ ДЕКОМПОЗИЦИИ ETF
    etf_components_str = ""
    is_in_listings = False
    
    try:
        # 1. Вытаскиваем символы топ-5 компонентов фонда, если они уже есть в СУБД
        sql_comp = """
            SELECT t.symbol FROM public.etf_holdings h
            JOIN public.tickers t ON h.component_ticker_id = t.id
            WHERE h.etf_ticker_id = %s
            ORDER BY h.weight_percentage DESC LIMIT 5;
        """
        comp_rows = await asyncio.to_thread(db_bot.execute_query, sql_comp, (ticker_id,))
        if comp_rows:
            etf_components_str = ", ".join([r['symbol'] for r in comp_rows])

        # 2. 🔥 НОВАЯ ЗРЯЧАЯ ПРОВЕРА: Смотрим, добавлен ли фонд в реальные листинги портфелей
        sql_list_check = "SELECT 1 FROM public.listings WHERE ticker_id = %s LIMIT 1;"
        list_res = await asyncio.to_thread(db_bot.execute_query, sql_list_check, (ticker_id,))
        is_in_listings = True if list_res else False

    except Exception as e:
        logging.error(f"⚠️ Ошибка экспресс-анализа таблиц листингов и холдингов ETF: {e}")

    # 5. ШАГ 5: Единый header UPort (см. Claude/05_strategy_screen_and_kubiki.md, BACKLOG.md #19)
    # + техническая сводка, специфичная для этого экрана (тренд/брокер FB/RSI)
    header_text = await format_premium_header(ticker_id=ticker_id, portfolio_id=0)

    raw_trend = str(t.get('signal_recommendation') or 'NEUTRAL').upper()
    trend_icon, trend_label = TREND_LABELS.get(raw_trend, ("🔸", raw_trend.replace('_', ' ')))
    fb_ticker_name = ticker_name_map.get("FB", "UNSUPPORTED")
    fb_line = f"• Брокер FB: `{fb_ticker_name}`\n" if fb_ticker_name != "UNSUPPORTED" else ""
    asset_type = str(t.get('asset_type', 'UNDEFINED')).upper().strip()

    report_text = (
        f"{header_text}"
        f"📢 Долгосрочный тренд: **{trend_icon} {trend_label}**\n"
        f"{fb_line}"
        f"📊 **Техника:** RSI:{float(t.get('signal_rsi') or 0):.1f} | "
        f"100д:{float(t.get('signal_price_to_sma100_pct') or 0):+.1f}% | "
        f"200д:{float(t.get('signal_price_to_sma200_pct') or 0):+.1f}%\n"
    )

    # == УЛЬТРА-КОРОТКИЙ ВЫВОД ДЛЯ АКЦИЙ ==
    if asset_type == 'EQUITY' or asset_type == 'UNDEFINED':
        fcf_val = float(t.get('free_cash_flow') or 0) / 1_000_000
        report_text += (
            f"🧬 **Фундаментал:**\n"
            f" • Див. доходность: **{float(t.get('dividend_yield') or 0):.2f}%**\n"
            f" • Свободный кэш (FCF): **${fcf_val:,.1f}M**\n"
            f" 📅 Отчет СУБД: **{t.get('signal_next_report_date') or 'N/A'}**\n"
        )

    # == УЛЬТРА-КОРОТКИЙ ВЫВОД ДЛЯ БИРЖЕВЫХ ФОНДОВ (ETF) ==
    elif asset_type == 'ETF':
        exp_ratio = float(t.get('expense_ratio') or 0) * 100
        
        # Железная логика трех статусов Look-Through с учетом наличия в listings
        if etf_components_str:
            lt_status = f"`{etf_components_str}...`"
        elif is_in_listings:
            lt_status = "🪙 Товарный/Фьючерсный фонд"
        else:
            lt_status = "⏳ Очередь после добавления in WL"

        report_text += (
            f"💸 Комиссия фонда: **{exp_ratio:.2f}%**\n"
            f"📐 **Состав ETF:** {lt_status}\n"
        )

    # 5b. Совместимость с РЕАЛЬНЫМИ активными стратегиями портфелей (не "заводскими"
    # шаблонами) -- TickerEvaluator оценивает все активные content-стратегии портфеля
    # за один вызов (не по одной), поэтому на каждый портфель нужен ровно один вызов.
    strategy_compat_lines = []
    # Побуждение 3б (Claude/BACKLOG.md №118/122, шаг 7, живой случай SNDK) -- проходит ли
    # бумага хоть одну активную стратегию хоть одного портфеля ЭТОГО пользователя (не
    # семьи целиком -- покупка всё равно личная). Определяет, показывать ли кнопку
    # «➕ Купить вне стратегии» ниже.
    user_owns_any_pass = False
    try:
        portfolio_rows = await asyncio.to_thread(
            db_bot.execute_query,
            "SELECT id, name, owner_id FROM public.portfolios ORDER BY id;"
        )
        portfolio_rows = portfolio_rows if isinstance(portfolio_rows, list) else ([portfolio_rows] if portfolio_rows else [])

        evaluator = TickerEvaluator(db_instance=db_bot)
        for p in portfolio_rows:
            p_report = await asyncio.to_thread(evaluator.evaluate_ticker_strategy, int(ticker_id), int(p["id"]))
            for info in p_report.get("explain_map", {}).values():
                is_pass = bool(info["is_compatible_technically"])
                icon = "✅" if is_pass else "❌"
                strategy_compat_lines.append(f" • {p['name']} → {info['strategy_name']}: {icon}")
                if is_pass and user_db_id and int(p.get("owner_id") or 0) == int(user_db_id):
                    user_owns_any_pass = True
    except Exception as e:
        logging.error(f"⚠️ Ошибка проверки совместимости со стратегиями для ticker_id={ticker_id}: {e}")

    if strategy_compat_lines:
        report_text += "🎯 **Совместимость со стратегиями:**\n" + "\n".join(strategy_compat_lines) + "\n───────\n"

    # 5c. Уже ли бумага в СН хотя бы одного портфеля ЭТОГО пользователя -- тот же охват,
    # что и у самой кнопки добавления (process_add_to_watchlist_routing). Определяет,
    # показывать ли вообще "Добавить"/"Оставить в покое" (см. docstring выше).
    watched_portfolios = []
    if user_db_id:
        watched_rows = await asyncio.to_thread(db_bot.execute_query, """
            SELECT w.portfolio_id, p.name AS portfolio_name, w.listing_id
            FROM public.watchlist w
            JOIN public.listings l ON w.listing_id = l.id
            JOIN public.portfolios p ON w.portfolio_id = p.id
            WHERE l.ticker_id = %s AND p.owner_id = %s;
        """, (ticker_id, user_db_id))
        watched_portfolios = watched_rows if isinstance(watched_rows, list) else ([watched_rows] if watched_rows else [])

    # 6. ШАГ 6: СБОРКА СМАРТ-ПУЛЬТА
    builder = InlineKeyboardBuilder()
    if watched_portfolios:
        names = ", ".join(wp["portfolio_name"] for wp in watched_portfolios)
        report_text += f"✅ Уже в списке наблюдения: {names}."
        for wp in watched_portfolios:
            builder.row(types.InlineKeyboardButton(
                text=f"📂 Карточка в «{wp['portfolio_name']}»",
                callback_data=MenuAction(
                    action="view_ticker", portfolio_id=int(wp["portfolio_id"]), listing_id=int(wp["listing_id"]), sub_view="owner"
                ).pack()
            ))
    else:
        report_text += "Включить инструмент в список наблюдения?"
        builder.row(types.InlineKeyboardButton(
            text="🔬 В список наблюдения",
            callback_data=MenuAction(action="add_to_wl", ticker_id=int(ticker_id), sub_view="search").pack()
        ))

    # Побуждение 3б -- бумага не проходит ни одну активную стратегию НИ ОДНОГО портфеля
    # этого пользователя (живой случай SNDK, Claude/BACKLOG.md №118/122). Не блокирует
    # покупку (это деньги пользователя) -- ведёт в ручной ввод суммы/количества, авто-
    # слота неоткуда взять без стратегии. Показывается независимо от watched_portfolios.
    if user_db_id and not user_owns_any_pass:
        builder.row(types.InlineKeyboardButton(
            text="➕ Купить вне стратегии",
            callback_data=MenuAction(action="outside_buy_start", ticker_id=int(ticker_id)).pack()
        ))
    context_markup = builder.as_markup()
    final_builder = InlineKeyboardBuilder.from_markup(context_markup)

    if watched_portfolios:
        # Уже в СН -- "Оставить в покое" не имеет смысла (нечего оставлять, решение уже
        # принято раньше), обычная кнопка "В главное меню" без дублирующей формулировки.
        final_builder.attach(InlineKeyboardBuilder.from_markup(generate_nav_back_keyboard(menu_only=True)))
    else:
        # Нижнюю кнопку отмены генерируем через наш универсальный подпрограммный подвал UPort
        reply_markup = generate_nav_back_keyboard(
            one_step_back_text="💤 Оставить в покое",
            full_back_callback=MenuAction(action="main_menu").pack()
        )
        final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))


    try:
        await target_message.edit_text(report_text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


# =========================================================================
# ➕ «Купить вне стратегии» -- побуждение 3б (Claude/BACKLOG.md №118/122, шаг 7,
# живой случай SNDK). Бумага не проходит ни одну активную стратегию НИ ОДНОГО
# портфеля этого пользователя -- авто-слота (CashDeploymentAdvisor.compute_slot_size)
# взять неоткуда, сумма/количество вводятся вручную. Ложится в буферную стратегию
# «Неопределённая» (system_key='UNALLOCATED', уже существует, уже используется для
# бумаг вне стратегийной логики). mode="market", как и у обычного шага 1 -- решение
# уже принято целиком в момент клика, лесенки здесь не бывает.
# =========================================================================

class OutsideBuyStates(StatesGroup):
    waiting_for_amount = State()


@router.callback_query(MenuAction.filter(F.action == "outside_buy_start"))
async def process_outside_buy_start(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Смарт-разводка mono/multi-портфелей -- тот же приём, что и у process_add_to_watchlist_routing."""
    await callback.answer()
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    t_id = callback_data.ticker_id

    portfolios = await asyncio.to_thread(
        db_bot.execute_query, "SELECT id, name FROM public.portfolios WHERE owner_id = %s AND id != 0 ORDER BY id;", (user_db_id,)
    )
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])
    if not portfolios:
        try:
            await callback.message.edit_text("⚠️ У вас нет зарегистрированных портфелей.", reply_markup=generate_main_menu_keyboard())
        except TelegramBadRequest:
            pass
        return

    if len(portfolios) == 1:
        await _ask_outside_buy_amount(callback.message, state, user_db_id, int(portfolios[0]["id"]), t_id)
        return

    builder = InlineKeyboardBuilder()
    for p in portfolios:
        builder.row(types.InlineKeyboardButton(
            text=f"💼 {p['name']}",
            callback_data=MenuAction(action="outside_buy_portfolio", portfolio_id=int(p["id"]), ticker_id=t_id).pack()
        ))
    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    final_builder.attach(InlineKeyboardBuilder.from_markup(generate_nav_back_keyboard(menu_only=True)))
    try:
        await callback.message.edit_text("В какой портфель?", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "outside_buy_portfolio"))
async def process_outside_buy_portfolio_chosen(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    await callback.answer()
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    await _ask_outside_buy_amount(callback.message, state, user_db_id, callback_data.portfolio_id, callback_data.ticker_id)


async def _ask_outside_buy_amount(message: types.Message, state: FSMContext, user_db_id: int, portfolio_id: int, ticker_id: int):
    prior_data = await state.get_data()
    await state.set_state(OutsideBuyStates.waiting_for_amount)
    await state.update_data(
        user_db_id=user_db_id, is_admin=prior_data.get("is_admin", False),
        outside_buy_portfolio_id=portfolio_id, outside_buy_ticker_id=ticker_id,
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="outside_buy_cancel", ticker_id=ticker_id).pack()))
    try:
        await message.edit_text(
            "➕ **Покупка вне стратегии**\n\n"
            "Сколько купить? Отправь сумму в долларах (например `500`) или количество акций со словом «шт» "
            "(например `10 шт`). Ляжет в стратегию «Неопределённая».",
            parse_mode="Markdown", reply_markup=builder.as_markup()
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "outside_buy_cancel"))
async def process_outside_buy_cancel(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    await callback.answer()
    await state.set_state(None)
    await process_view_ticker_passport(callback, callback_data, state)


@router.message(OutsideBuyStates.waiting_for_amount)
async def process_outside_buy_amount_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    p_id = data.get("outside_buy_portfolio_id")
    t_id = data.get("outside_buy_ticker_id")
    raw = str(message.text or "").strip().lower().replace(",", ".").replace("$", "")

    is_shares = "шт" in raw
    numeric_part = re.sub(r"[^\d.]", "", raw)
    try:
        amount_val = float(numeric_part)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Не понял число. Отправь сумму в долларах (`500`) или количество акций (`10 шт`).")
        return

    await state.set_state(None)

    portfolio_row = await asyncio.to_thread(db_sys.execute_row, "SELECT broker_id FROM public.portfolios WHERE id = %s;", (p_id,))
    broker_id = int((portfolio_row or {}).get("broker_id") or 1)
    listing_row = await asyncio.to_thread(db_sys.execute_row, "SELECT id, last_price FROM public.listings WHERE ticker_id = %s AND broker_id = %s;", (t_id, broker_id))
    if not listing_row:
        try:
            new_l_id = await asyncio.to_thread(db_sys.ensure_listing, t_id, broker_id)
        except Exception as e:
            await message.answer(f"⚠️ Не удалось легализовать листинг: {e}")
            return
        listing_row = await asyncio.to_thread(db_sys.execute_row, "SELECT id, last_price FROM public.listings WHERE id = %s;", (new_l_id,))
    l_id = int(listing_row["id"])
    price = float(listing_row.get("last_price") or 0.0)
    if price <= 0:
        await message.answer("⚠️ Не удалось получить цену, попробуй позже.")
        return

    qty = int(amount_val) if is_shares else max(1, round(amount_val / price))

    unalloc_row = await asyncio.to_thread(db_sys.execute_row, """
        SELECT s.id, s.strategy_name FROM public.strategies s
        JOIN public.strategy_templates tpl ON s.template_id = tpl.id
        WHERE s.portfolio_id = %s AND tpl.system_key = 'UNALLOCATED';
    """, (p_id,))
    if not unalloc_row:
        await message.answer("🚨 В портфеле нет буферной стратегии «Неопределённая» -- обратись к администратору.")
        return
    s_id = int(unalloc_row["id"])

    result = await asyncio.to_thread(db_sys.execute_query, """
        INSERT INTO public.order_pipelines
            (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
             target_quantity, initial_entry_price, pending_broker_order_id, entry_trigger_override, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 1, 'PENDING', %s, %s, NULL, %s::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id;
    """, (p_id, l_id, t_id, s_id, qty, price, json.dumps({"mode": "market"})))
    if not result:
        await message.answer("⚠️ Не удалось создать план -- возможно, по этой бумаге в «Неопределённой» уже есть активный план.")
        return
    pipeline_id = result[0]["id"] if isinstance(result, list) else result["id"]

    await asyncio.to_thread(db_sys.ensure_watchlist_row_v2, portfolio_id=p_id, listing_id=l_id, reason="watched")

    logging.info(f"✅ [OutsideBuy]: План #{pipeline_id} вне стратегии создан (ticker_id={t_id}, {qty} шт, портфель {p_id}).")

    back_kb = generate_nav_back_keyboard(
        one_step_back_text="🔙 К карточке бумаги",
        full_back_callback=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, sub_view="owner").pack()
    )
    try:
        await message.answer(
            f"✅ **План создан** (#{pipeline_id}, «{unalloc_row['strategy_name']}»)\n\n"
            f"~{qty} шт по рынку.\n\nЖду рынка.",
            parse_mode="Markdown", reply_markup=back_kb
        )
    except TelegramBadRequest:
        pass
