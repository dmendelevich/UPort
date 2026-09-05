import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_nav_back_keyboard, generate_portfolio_button_text, generate_tab_switch_keyboard, generate_strategy_button_text, generate_main_menu_keyboard
from bot_handlers.bot_screens import format_portfolio_risk_audit_rollup, format_portfolio_header

from analytics.portfolio_inspector import PortfolioInspector

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

    logging.debug(f"💼 [ПОРТФЕЛЬ]: Сборка аналитического паспорта для portfolio_id = {p_id}, вкладка = '{view}'")
    await callback.answer("Сборка аналитического паспорта...")

    # Мультивалютность: та же схема, что у карточек тикера/стратегии -- всё выводим в
    # base_currency СМОТРЯЩЕГО пользователя, не в родной валюте листинга. target_currency/
    # target_sign передаются ниже в format_portfolio_header, которая сама кэширует курс по
    # коду валюты (источников валют может быть НЕСКОЛЬКО РАЗНЫХ, у каждой бумаги своя) --
    # эта функция больше не считает fx сама (вынесено вместе с хедером, 2026-08-15).
    user_data = await state.get_data()
    user_db_id = user_data.get("user_db_id")
    target_currency = "USD"
    if user_db_id:
        user_row = await asyncio.to_thread(db_bot.execute_row, "SELECT base_currency FROM public.users WHERE id = %s;", (user_db_id,))
        if user_row and user_row.get("base_currency"):
            target_currency = user_row["base_currency"]

    target_cur_row = await asyncio.to_thread(db_bot.execute_row, "SELECT sign FROM public.currencies WHERE id = %s;", (target_currency,))
    target_sign = target_cur_row.get('sign', '$') if target_cur_row else '$'

    # 1. ХЕДЕР -- общий самодостаточный кубик (bot_handlers/bot_screens.py::format_portfolio_header),
    # тот же самый, что теперь показывает и вкладка «Дайджест» (bot_handlers/digest.py,
    # тема «дайджест как вкладка», 2026-08-15). Раньше "портфель не найден" проверялся
    # ОТДЕЛЬНЫМ тяжёлым вызовом generate_portfolio_passport (весь аудит фундаментала
    # ради одной строки-флага) ДО хедера -- нестандартно, не так, как у стратегии
    # (format_strategy_header сам возвращает "не найдена" текстом). Приведено к тому
    # же самодостаточному паттерну (2026-08-15, по замечанию пользователя) --
    # generate_portfolio_passport здесь больше не нужен вообще, убран.
    report_text = await format_portfolio_header(p_id, target_currency=target_currency, target_sign=target_sign)
    if "не найден" in report_text:
        try:
            await callback.message.edit_text(report_text, reply_markup=generate_main_menu_keyboard())
        except TelegramBadRequest:
            pass
        return

    # 2. ПРЕДВАРИТЕЛЬНЫЙ РАСЧЕТ И СБОР ДАННЫХ ПО АКЦИЯМ (С ЧЕСТНЫМ ПОДСЧЕТОМ АЛЕРТОВ 3NF) --
    # нужен только вкладке "Состав" ниже, хедер выше их не переиспользует (собирает свои).
    assets_query = """
        SELECT l.broker_symbol AS full_ticker, a.quantity, a.avg_price, l.last_price, a.listing_id, l.currency_id,
               EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days,
               COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
        FROM public.assets a
        JOIN public.listings l ON a.listing_id = l.id
        LEFT JOIN public.alerts al ON al.listing_id = a.listing_id
                                  AND al.portfolio_id = a.portfolio_id
                                  AND al.is_active = true
        WHERE a.portfolio_id = %s AND a.quantity > 0
        GROUP BY l.broker_symbol, a.quantity, a.avg_price, l.last_price, a.listing_id, l.currency_id, a.position_opened_at
        ORDER BY l.broker_symbol ASC;
    """
    assets_res_raw = db_bot.execute_query(assets_query, (p_id,))
    assets_res = assets_res_raw if isinstance(assets_res_raw, list) else ([assets_res_raw] if assets_res_raw else [])

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
        report_text += "🩻 **Паспорт качества:**\n"
        if p_id > 0:
            report_text += await format_portfolio_risk_audit_rollup(p_id)
        report_text += (
            "🚧 Остальные аспекты качества (риск от каждой стратегии, от их\n"
            "совокупности и от набора бумаг вне стратегий) — отдельная тема позже.\n"
        )

    elif view == "strategies":
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
            # Динамический список id стратегий -- IN не работает через JSON-параметризацию
            # (см. Claude/BACKLOG.md №81) -- ANY(%s).
            strategy_ids = [int(sid) for sid in strat_map.keys()]
            key_rows = await asyncio.to_thread(
                db_bot.execute_query,
                """
                    SELECT s.id, s.is_screening_active, st.system_key FROM public.strategies s
                    JOIN public.strategy_templates st ON s.template_id = st.id
                    WHERE s.id = ANY(%s);
                """,
                (strategy_ids,)
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
        ("🩻 Паспорт качества", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view=f"passport/{origin}")),
        ("🎯 Стратегии", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view=f"strategies/{origin}")),
        ("✅ Дела", MenuAction(action="view_digest", portfolio_id=p_id, sub_view="overview")),
    ]

    tab_switch_markup = generate_tab_switch_keyboard(tabs, current_sub_view=f"{view}/{origin}")
    for tab_row in tab_switch_markup.inline_keyboard:
        builder.row(*tab_row)

    # Кнопка возобновления автопокупок (Claude/BACKLOG.md №170, 2026-09-05) -- видна
    # ТОЛЬКО когда сработал VIX-предохранитель полигона (analytics/polygon_paper_trader.py).
    # Возобновление сознательно ручное, не автоматическое по возврату VIX в диапазон --
    # тема #168 показала, что авто-откат по рыночному сигналу сам может дать whipsaw.
    portfolio_pause_row = await asyncio.to_thread(
        db_bot.execute_row, "SELECT auto_trading_paused FROM public.portfolios WHERE id = %s;", (p_id,)
    )
    if portfolio_pause_row and portfolio_pause_row.get("auto_trading_paused"):
        builder.row(types.InlineKeyboardButton(
            text="▶️ Возобновить автопокупки",
            callback_data=MenuAction(action="resume_polygon_trading", portfolio_id=p_id).pack()
        ))

    # Функциональная кнопка (Claude/05_strategy_screen_and_kubiki.md: после переключателя
    # вкладок, перед навигацией назад) -- Лист ожидания (Claude/BACKLOG.md, 2026-08-28),
    # заменяет прежний вход через СН ("🔬 Списки наблюдения" в главном меню, убран).
    builder.row(types.InlineKeyboardButton(
        text="⏳ Лист ожидания",
        callback_data=MenuAction(action="view_pending_plans", portfolio_id=p_id).pack()
    ))

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

    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=final_builder.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "resume_polygon_trading"))
async def process_resume_polygon_trading(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """
    Ручное возобновление автопокупок после срабатывания VIX-предохранителя
    (Claude/BACKLOG.md №170, analytics/polygon_paper_trader.py). Сознательно ручное
    действие -- система сама пометит паузу true в следующий раз, если VIX снова
    выйдет за диапазон, но снять её сама не может (это и есть весь смысл
    предохранителя: решение о продолжении принимает человек, не автоматика).
    """
    # НЕ отвечаем на callback здесь -- process_view_portfolio ниже сам вызывает
    # callback.answer() при перерисовке карточки; второй answerCallbackQuery на тот
    # же callback_query_id Telegram обычно отклоняет (живая находка при мок-тесте
    # 2026-09-05 -- unittest.mock тихо пропустил повторный вызов, реальный API нет).
    p_id = callback_data.portfolio_id
    await asyncio.to_thread(
        db_bot.execute_query,
        "UPDATE public.portfolios SET auto_trading_paused = false, auto_trading_paused_at = NULL, "
        "auto_trading_paused_reason = NULL WHERE id = %s;",
        (p_id,)
    )
    await process_view_portfolio(callback, MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets"), state)
