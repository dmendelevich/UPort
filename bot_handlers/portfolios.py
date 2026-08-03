import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import build_smart_badge, generate_nav_back_keyboard, generate_portfolio_button_text, generate_tab_switch_keyboard, generate_strategy_button_text, generate_main_menu_keyboard
from bot_handlers.bot_screens import format_portfolio_risk_audit_rollup

# Импортируем независимый аналитический модуль аудитора портфеля
from analytics.portfolio_auditor import generate_portfolio_passport
from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import convert_currency_amount, CONTENT_STRATEGY_SYSTEM_KEYS

# Инициализируем локальный роутер для модуля портфелей
router = Router()

# Короткие однословные подписи для кнопок списка стратегий -- полное название
# (strategy_name, свободный текст, пользователь может его переименовать как угодно)
# не помещалось на экране телефона и обрезалось. Ключ -- system_key (стабильный,
# не зависит от переименования), а не текст strategy_name.
SHORT_STRATEGY_LABELS = {
    "REVOLVER": "Револьверная",
    "CONSERVATIVE_ACCUMULATION": "Консервативная",
    "TREND_FOLLOWING": "Трендовая",
    "CASH_RESERVE": "Кэш/Резерв",
    "UNALLOCATED": "Неопределённая",
}

# def get_superscript_badge(count: int) -> str:
#     """Вспомогательный конвертер чисел в аккуратные суперскрипт-индикаторы для кошелька."""
#     if count <= 0:
#         return ""
#     superscripts = {
#         '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
#         '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
#     }
#     return "🔔" + "".join(superscripts.get(char, char) for char in str(count))

@router.callback_query(MenuAction.filter(F.action == "view_portfolio"))
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Экран Уровня 2: Детализация конкретного счета семьи (Интерфейс шторок Состав / Паспорт)."""
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view or "assets"  # по умолчанию -- "Состав портфеля" (см. фидбек 2026-07-27)

    # Составной sub_view с происхождением (тот же приём, что у шторки алертов, см.
    # tickers.py) -- "assets/test_capital" значит "пришли из 🧪 Тестового капитала",
    # тогда кнопка "назад" должна вести туда, а не в реальную сводку по умолчанию.
    origin = "summary"
    if "/" in view:
        view, origin = view.split("/", 1)

    print(f"\n💼 [ПОРТФЕЛЬ ТРИГГЕР]: Сборка аналитического паспорта для portfolio_id = {p_id}, вкладка = '{view}'")
    await callback.answer("Сборка аналитического паспорта...")

    # Мультивалютность: та же схема, что у карточек тикера/стратегии -- всё выводим в
    # base_currency СМОТРЯЩЕГО пользователя, не в родной валюте листинга. Здесь источников
    # валют может быть НЕСКОЛЬКО РАЗНЫХ (у каждой бумаги своя) в одном рендере, поэтому курс
    # кэшируем по коду валюты (fx_by_currency), а не считаем один общий rate, как у тикера.
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    target_currency = "USD"
    if user_db_id:
        user_row = await asyncio.to_thread(db_bot.execute_row, f"SELECT base_currency FROM public.users WHERE id = {int(user_db_id)};")
        if user_row and user_row.get("base_currency"):
            target_currency = user_row["base_currency"]

    target_cur_row = await asyncio.to_thread(db_bot.execute_row, f"SELECT sign FROM public.currencies WHERE id = '{target_currency}';")
    target_sign = target_cur_row.get('sign', '$') if target_cur_row else '$'
    fx_by_currency = {}

    def get_fx(from_currency: str) -> float:
        cur = from_currency or "USD"
        if cur not in fx_by_currency:
            fx_by_currency[cur] = convert_currency_amount(db_bot, 1.0, cur, target_currency)
        return fx_by_currency[cur]

    # 1. Вызываем модуль аудитора для расчета рисков и соответствия стратегии
    passport = await asyncio.to_thread(generate_portfolio_passport, p_id, db_bot)
    if not passport:
        print(f"❌ [ПОРТФЕЛЬ ОШИБКА]: Не удалось сгенерировать паспорт для id = {p_id}")
        await callback.message.edit_text("❌ Ошибка генерации паспорта портфеля.", reply_markup=generate_main_menu_keyboard())
        return

    meta = passport["meta"]

    # Счётчик стратегий для шапки -- только СОДЕРЖАТЕЛЬНЫЕ (Кэш/Резерв и Неопределённая
    # не считаются, у портфеля нет единой "стратегии", legacy-поле strategy_type изживаем)
    content_strategies_count = 0
    if p_id > 0:
        keys_str = ", ".join(f"'{k}'" for k in CONTENT_STRATEGY_SYSTEM_KEYS)
        count_row = await asyncio.to_thread(
            db_bot.execute_row,
            f"""
                SELECT COUNT(*)::int AS cnt FROM public.strategies s
                JOIN public.strategy_templates st ON s.template_id = st.id
                WHERE s.portfolio_id = {int(p_id)} AND s.is_active = true AND st.system_key IN ({keys_str});
            """
        )
        content_strategies_count = count_row.get("cnt", 0) if count_row else 0

    # 2. ПРЕДВАРИТЕЛЬНЫЙ РАСЧЕТ И СБОР ДАННЫХ ПО АКЦИЯМ (С ЧЕСТНЫМ ПОДСЧЕТОМ АЛЕРТОВ 3NF)
    assets_query = f"""
        SELECT l.broker_symbol AS full_ticker, a.quantity, a.avg_price, l.last_price, a.listing_id, l.currency_id,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days,
               COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
        FROM public.assets a
        JOIN public.listings l ON a.listing_id = l.id
        LEFT JOIN public.alerts al ON al.listing_id = a.listing_id
                                  AND al.portfolio_id = a.portfolio_id
                                  AND al.is_active = true
        WHERE a.portfolio_id = {p_id} AND a.quantity > 0
        GROUP BY l.broker_symbol, a.quantity, a.avg_price, l.last_price, a.listing_id, l.currency_id, a.position_opened_at
        ORDER BY l.broker_symbol ASC;
    """
    # Только торговый счёт ЭТОГО портфеля -- накопительный принадлежит владельцу, а не
    # портфелю (один накопительный на нескольких портфелях одного брокера был бы задвоен),
    # полная раскладка по всем счетам переехала в отдельный экран "🏦 Счета" (см. summary.py)
    cash_query = f"""
        SELECT a.currency_id, a.cash_available, a.cash_reserved, cur.sign
        FROM public.accounts a
        JOIN public.currencies cur ON a.currency_id = cur.id
        WHERE a.portfolio_id = {p_id} AND a.account_type = 'trade';
    """

    # Делаем вызовы через права бота db_bot
    assets_res_raw = db_bot.execute_query(assets_query)
    assets_res = assets_res_raw if isinstance(assets_res_raw, list) else ([assets_res_raw] if assets_res_raw else [])

    # Считаем совокупные финансовые показатели акций -- каждая позиция сначала конвертируется
    # из СВОЕЙ родной валюты (l.currency_id) в валюту смотрящего, и только потом суммируется
    # (раньше складывались сырые цифры разных валют как будто это одна и та же валюта).
    total_assets_cost = 0.0
    total_assets_profit = 0.0

    for asset in assets_res:
        qty = float(asset['quantity'] or 0)
        avg_p = float(asset['avg_price'] or 0)
        last_p = float(asset['last_price'] or 0)
        fx_rate = get_fx(asset.get('currency_id'))

        cost_basis = qty * avg_p * fx_rate
        market_val = qty * last_p * fx_rate
        profit = market_val - cost_basis

        total_assets_cost += market_val
        total_assets_profit += profit

    total_profit_pct = (total_assets_profit / (total_assets_cost - total_assets_profit) * 100) if (total_assets_cost - total_assets_profit) > 0 else 0.0
    profit_sign = "+" if total_assets_profit >= 0 else "-"

    # 3. ФОРМИРОВАНИЕ СТЕРИЛЬНОЙ И ДОРОГОЙ ШАПКИ
    report_text = f"📦 **ПОРТФЕЛЬ {meta.get('name', '')}**\n"
    report_text += f"👤 {meta.get('owner', 'Unknown')}\n"
    report_text += f"🎯 Стратегии: **{content_strategies_count}**\n"

    report_text += f"───────\n"
    report_text += f"📊 **АКТИВЫ В БУМАГАХ:**\n"
    report_text += f"• Общая стоимость: **{target_sign}{total_assets_cost:,.2f}**\n"
    report_text += f"• Чистая прибыль: **{profit_sign}{target_sign}{abs(total_assets_profit):,.2f} ({profit_sign}{abs(total_profit_pct):.1f}%)**\n\n"

    # Сборка мультивалютного кэша семьи
    cash_res_raw = db_bot.execute_query(cash_query)
    cash_res = cash_res_raw if isinstance(cash_res_raw, list) else ([cash_res_raw] if cash_res_raw else [])
    
    trade_cash_lines = []
    for c in cash_res:
        available = float(c['cash_available'])
        reserved = float(c['cash_reserved'])
        if available == 0 and reserved == 0:
            continue
        free = available - reserved
        o_sign = c['sign'] or c['currency_id'] or "$"
        trade_cash_lines.append(f"• {c['currency_id']}: **{o_sign}{available:,.2f}** (🕊️ {o_sign}{free:,.2f} / 🔒 {o_sign}{reserved:,.2f})")

    if trade_cash_lines:
        report_text += "💵 **Кэш на торговом счёте:**\n"
        report_text += "\n".join(trade_cash_lines) + "\n\n"

    report_text += f"───────\n"

    builder = InlineKeyboardBuilder()

    # 4. ЛОГИКА ОТРЕНДЕРИВАНИЯ ВНУТРЕННОСТЕЙ ШТОРОК
    if view == "assets":
        report_text += "📦 **Текущий состав ценных бумаг:**"
        
        if assets_res:
            for asset in assets_res:
                broker_ticker = asset['full_ticker']
                qty = float(asset['quantity'] or 0)
                avg_p = float(asset['avg_price'] or 0)
                last_p = float(asset['last_price'] or 0)
                days = int(asset['holding_days'] or 0)
                
                clean_ticker = broker_ticker[:-3] if broker_ticker.endswith(".US") else broker_ticker
                
                position_cost = qty * avg_p
                position_market = qty * last_p
                position_profit = position_market - position_cost
                position_profit_pct = (position_profit / position_cost * 100) if position_cost > 0 else 0.0
                
                if position_profit > 0.01:
                    crystal = "🟢"
                    p_sign = "+"
                elif position_profit < -0.01:
                    crystal = "🔴"
                    p_sign = ""
                else:
                    crystal = "🔹"
                    p_sign = "+"

                clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"
                
                # 🔥 РЕФАКТОРИНГ: Полностью переводим на жесткую модульную LEGO-сетку UPort
                # Вызываем высокоуровневый генератор строки кнопки портфеля
                button_text = generate_portfolio_button_text(
                    crystal=crystal,
                    ticker=clean_ticker,
                    quantity=int(qty),
                    profit=position_profit,
                    profit_pct=position_profit_pct
                )
                
                l_id = int(asset.get('listing_id') or 0)
                
                # При клике на акцию из капитала — прыгаем на главный экран владения тикера (sub_view='owner')
                builder.row(types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=MenuAction(
                        action="view_ticker", 
                        portfolio_id=p_id, 
                        listing_id=l_id, 
                        ticker_name=broker_ticker,
                        sub_view="owner"
                    ).pack()
                ))        
        else:
            report_text += "\n   *Ценные бумаги в данном портфеле отсутствуют.*"

    elif view == "passport":
        report_text += "📊 **Паспорт качества:**\n"
        if p_id > 0:
            report_text += await format_portfolio_risk_audit_rollup(p_id)
        report_text += (
            "🚧 Остальные аспекты качества (риск от каждой стратегии, от их\n"
            "совокупности и от набора бумаг вне стратегий) — отдельная тема позже.\n"
        )

    elif view == "strategies":
        print("🎯 [ПОРТФЕЛЬ]: Рендеринг списка активных стратегий...")
        report_text += "🎯 **Активные стратегии портфеля:**"

        inspector = await asyncio.to_thread(PortfolioInspector, db_bot, p_id)
        balances = await asyncio.to_thread(inspector.get_virtual_cash_balances)
        total_capital = float(balances.get("total_capital_usd") or 0.0)
        strat_map = balances.get("strategies", {})

        # Короткие подписи ищутся по system_key -- один запрос на весь список,
        # не по одному на стратегию. is_screening_active -- пассивные стратегии
        # заводского набора (см. Claude/BACKLOG.md п.9, 2026-07-31) помечаются
        # отдельно, чтобы 0%/0% не выглядело как баг.
        system_key_by_id = {}
        screening_active_by_id = {}
        if strat_map:
            ids_str = ", ".join(str(int(sid)) for sid in strat_map.keys())
            key_rows = await asyncio.to_thread(
                db_bot.execute_query,
                f"""
                    SELECT s.id, s.is_screening_active, st.system_key FROM public.strategies s
                    JOIN public.strategy_templates st ON s.template_id = st.id
                    WHERE s.id IN ({ids_str});
                """
            )
            key_rows = key_rows if isinstance(key_rows, list) else ([key_rows] if key_rows else [])
            system_key_by_id = {int(r["id"]): r.get("system_key") for r in key_rows if r}
            screening_active_by_id = {int(r["id"]): bool(r.get("is_screening_active")) for r in key_rows if r}

        if strat_map:
            for s_id, s in strat_map.items():
                target_pct = float(s["target_share_pct"])
                current_usd = float(s["current_holdings_usd"])
                actual_pct = (current_usd / total_capital * 100.0) if total_capital > 0 else 0.0
                system_key = system_key_by_id.get(int(s_id))
                short_name = SHORT_STRATEGY_LABELS.get(system_key, s['strategy_name'])
                is_screening_active = screening_active_by_id.get(int(s_id), True)
                button_text = generate_strategy_button_text(
                    name=short_name,
                    target_pct=target_pct,
                    actual_pct=actual_pct,
                    icon="🎯" if is_screening_active else "😴"
                )
                builder.row(types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=MenuAction(action="view_strategy", strategy_id=int(s_id), portfolio_id=p_id).pack()
                ))
        else:
            report_text += "\n   *Активные стратегии в этом портфеле не найдены.*"

    # Динамический пульт шторки: переключатель вкладок через общий кубик-сборщик (см. Claude/BACKLOG.md #13)
    # Происхождение (origin) переносится на КАЖДУЮ вкладку, иначе переключение вкладки
    # потеряло бы, откуда пришли, и "назад" снова вело бы не туда.
    tabs = [
        ("📦 Состав портфеля", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view=f"assets/{origin}")),
        ("📊 Паспорт качества", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view=f"passport/{origin}")),
        ("🎯 Стратегии", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view=f"strategies/{origin}")),
    ]

    tab_switch_markup = generate_tab_switch_keyboard(tabs, current_sub_view=f"{view}/{origin}")
    for tab_row in tab_switch_markup.inline_keyboard:
        builder.row(*tab_row)

    # Блок навигации назад -- по умолчанию в реальную сводку, но если пришли из
    # 🧪 Тестового капитала, ведём обратно туда (BACKLOG.md, стандартизация 2026-08-03).
    tabs_markup = builder.as_markup()

    back_action = "show_test_summary" if origin == "test_capital" else "show_summary"
    back_text = "🔙 К тестовому капиталу" if origin == "test_capital" else "🔙 К общей сводке"
    reply_markup = generate_nav_back_keyboard(
        one_step_back_text=back_text,
        full_back_callback=MenuAction(action=back_action).pack()
    )
    
    # Объединяем кнопки вкладок и кнопки универсального возврата UPort
    final_builder = InlineKeyboardBuilder.from_markup(tabs_markup)
    final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))

    print("🖥️ [ПОРТФЕЛЬ]: Отправляю собранную шторку в Telegram...")
    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=final_builder.as_markup())        
    except TelegramBadRequest:
        pass
