import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import build_smart_badge, generate_nav_back_keyboard, generate_portfolio_button_text, generate_tab_switch_keyboard, generate_strategy_button_text, generate_main_menu_keyboard

# Импортируем независимый аналитический модуль аудитора портфеля
from analytics.portfolio_auditor import generate_portfolio_passport
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
async def process_view_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    """Экран Уровня 2: Детализация конкретного счета семьи (Интерфейс шторок Состав / Паспорт)."""
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view  # 'assets', 'passport' или "" (разводка по умолчанию)
    
    print(f"\n💼 [ПОРТФЕЛЬ ТРИГГЕР]: Сборка аналитического паспорта для portfolio_id = {p_id}, вкладка = '{view}'")
    await callback.answer("Сборка аналитического паспорта...")

    # 1. Вызываем модуль аудитора для расчета рисков и соответствия стратегии
    passport = await asyncio.to_thread(generate_portfolio_passport, p_id, db_bot)
    if not passport:
        print(f"❌ [ПОРТФЕЛЬ ОШИБКА]: Не удалось сгенерировать паспорт для id = {p_id}")
        await callback.message.edit_text("❌ Ошибка генерации паспорта портфеля.", reply_markup=generate_main_menu_keyboard())
        return

    meta = passport["meta"]
    
    # 2. ПРЕДВАРИТЕЛЬНЫЙ РАСЧЕТ И СБОР ДАННЫХ ПО АКЦИЯМ (С ЧЕСТНЫМ ПОДСЧЕТОМ АЛЕРТОВ 3NF)
    if p_id == 0:
        # Сценарий Б: Сводный портфель семьи — суммируем вообще все активные алерты клана
        assets_query = """
            SELECT l.broker_symbol AS full_ticker, SUM(a.quantity) as quantity,
                   MIN(a.listing_id) as listing_id,
                   AVG(a.avg_price) as avg_price, AVG(l.last_price) as last_price,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(MAX(a.position_opened_at), CURRENT_TIMESTAMP)))::int AS holding_days,
                   COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
            FROM public.assets a 
            JOIN public.listings l ON a.listing_id = l.id
            LEFT JOIN public.alerts al ON al.listing_id = l.id AND al.is_active = true
            WHERE a.quantity > 0 
            GROUP BY l.broker_symbol 
            ORDER BY l.broker_symbol ASC;
        """
        cash_query = """
            SELECT p.name AS portfolio_name, u.name AS owner_name, acc.account_type, 
                   acc.currency_id, cur.sign, acc.cash_available, acc.cash_reserved
            FROM public.accounts acc
            JOIN public.users u ON acc.user_id = u.id
            JOIN public.currencies cur ON acc.currency_id = cur.id
            LEFT JOIN public.portfolios p ON acc.portfolio_id = p.id
            ORDER BY acc.account_type ASC, u.id ASC;
        """
    else:
        # Сценарий А: Частный счет — считаем алерты строго этого портфеля
        assets_query = f"""
            SELECT l.broker_symbol AS full_ticker, a.quantity, a.avg_price, l.last_price, a.listing_id,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(a.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days,
                   COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
            FROM public.assets a 
            JOIN public.listings l ON a.listing_id = l.id
            LEFT JOIN public.alerts al ON al.listing_id = a.listing_id 
                                      AND al.portfolio_id = a.portfolio_id 
                                      AND al.is_active = true
            WHERE a.portfolio_id = {p_id} AND a.quantity > 0 
            GROUP BY l.broker_symbol, a.quantity, a.avg_price, l.last_price, a.listing_id, a.position_opened_at
            ORDER BY l.broker_symbol ASC;
        """
        cash_query = f"""
            SELECT a.account_number, a.account_type, a.currency_id, a.cash_available, a.cash_reserved, cur.sign
            FROM public.accounts a
            JOIN public.currencies cur ON a.currency_id = cur.id
            WHERE a.portfolio_id = {p_id}
               OR (a.account_type = 'deposit' AND a.user_id = (SELECT owner_id FROM public.portfolios WHERE id = {p_id}));
        """

    # Делаем вызовы через права бота db_bot
    assets_res_raw = db_bot.execute_query(assets_query)
    assets_res = assets_res_raw if isinstance(assets_res_raw, list) else ([assets_res_raw] if assets_res_raw else [])

    # Считаем совокупные финансовые показатели акций
    total_assets_cost_usd = 0.0
    total_assets_profit_usd = 0.0
    
    for asset in assets_res:
        qty = float(asset['quantity'] or 0)
        avg_p = float(asset['avg_price'] or 0)
        last_p = float(asset['last_price'] or 0)
        
        cost_basis = qty * avg_p
        market_val = qty * last_p
        profit = market_val - cost_basis
        
        total_assets_cost_usd += market_val
        total_assets_profit_usd += profit

    total_profit_pct = (total_assets_profit_usd / (total_assets_cost_usd - total_assets_profit_usd) * 100) if (total_assets_cost_usd - total_assets_profit_usd) > 0 else 0.0
    profit_sign = "+" if total_assets_profit_usd >= 0 else ""

    # 3. ФОРМИРОВАНИЕ СТЕРИЛЬНОЙ И ДОРОГОЙ ШАПКИ
    if p_id == 0:
        report_text = "📦 **Портфель сводный**\n"
    else:
        report_text = f"📦 **ПОРТФЕЛЬ {meta.get('name', '')}**\n"
        report_text += f"👤 {meta.get('owner', 'Unknown')}\n"
        report_text += f"💼 `{meta.get('strategy', 'Not Set')}`\n"

    report_text += f"───────\n"
    report_text += f"📊 **АКТИВЫ В БУМАГАХ:**\n"
    report_text += f"• Общая стоимость: **${total_assets_cost_usd:,.2f}**\n"
    report_text += f"• Чистая прибыль: **{profit_sign}${total_assets_profit_usd:,.2f} ({profit_sign}{total_profit_pct:.1f}%)**\n\n"

    # Сборка мультивалютного кэша семьи
    cash_res_raw = db_bot.execute_query(cash_query)
    cash_res = cash_res_raw if isinstance(cash_res_raw, list) else ([cash_res_raw] if cash_res_raw else [])
    
    trade_cash_lines = []
    aggregated_deposits = {}
    
    for c in cash_res:
        available = float(c['cash_available'])
        reserved = float(c['cash_reserved'])
        free = available - reserved
        o_sign = c['sign'] or c['currency_id'] or "$"
        
        if c['account_type'] == 'trade' and (available != 0 or reserved != 0):
            if p_id == 0:
                p_title = c.get('portfolio_name', 'Неизвестный')
                trade_cash_lines.append(f"• {p_title}: **{o_sign}{available:,.2f}** (🕊️ {o_sign}{free:,.2f} / 🔒 {o_sign}{reserved:,.2f})")
            else:
                trade_cash_lines.append(f"• {c['currency_id']}: **{o_sign}{available:,.2f}** (🕊️ {o_sign}{free:,.2f} / 🔒 {o_sign}{reserved:,.2f})")
                
        elif c['account_type'] in ('deposit', 'D') and available != 0:
            if p_id == 0:
                owner = c.get('owner_name', 'Неизвестный')
                key = (owner, o_sign)
                aggregated_deposits[key] = aggregated_deposits.get(key, 0.0) + available
            else:
                key = (meta.get('owner', 'Unknown'), o_sign)
                aggregated_deposits[key] = aggregated_deposits.get(key, 0.0) + available

    if p_id == 0:
        if trade_cash_lines:
            report_text += "💵 **КЭШ НА ТОРГОВЫХ СЧЕТАХ:**\n"
            report_text += "\n".join(trade_cash_lines) + "\n\n"
        if aggregated_deposits:
            report_text += "💰 **НАКОПИТЕЛЬНЫЕ D-СЧЕТА:**\n"
            for (owner, sign), total_dep_val in aggregated_deposits.items():
                report_text += f"• {owner}: **{sign}{total_dep_val:,.2f}**\n"
            report_text += "\n"
    else:
        if trade_cash_lines or aggregated_deposits:
            report_text += "💵 **КЭШ НА ТОРГОВЫХ СЧЕТАХ:**\n"
            if trade_cash_lines:
                report_text += "\n".join(trade_cash_lines) + "\n\n"
            if aggregated_deposits:
                report_text += "💰 **НАКОПИТЕЛЬНЫЕ D-СЧЕТА:**\n"
                for (owner, sign), total_dep_val in aggregated_deposits.items():
                    report_text += f"• {sign}{total_dep_val:,.2f}\n"

    if view == "passport":
        report_text += f"───────\n"
        report_text += f"🛡️ **Соответствие стратегии {meta['name']}:**\n"
        if passport["violations"]:
            for v in passport["violations"]:
                report_text += f" {v}\n"
        else:
            report_text += " ✅ Все лимиты и налоговые риски портфеля соответствуют стратегии.\n"

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
        print("📊 [ПОРТФЕЛЬ]: Рендеринг усредненного фундаментального паспорта Yahoo...")
        avg = passport["averages"]
        report_text += (
            f"📊 **Паспорт качества портфеля (Yahoo):**\n"
            f" • Ср. окупаемость P/E: **{avg['pe_trailing']:.1f}** | Прогноз: **{avg['pe_forward']:.1f}**\n"
            f" • Коэффициент PEG: **{avg['peg_ratio']:.2f}**\n"
            f" • Цена к выручке P/S: **{avg['price_to_sales']:.1f}** | К балансу P/B: **{avg['price_to_book']:.1f}**\n"
            f" • Стоимость бизнеса EV/EBITDA: **{avg['ev_to_ebitda']:.1f}**\n"
            f" ───────────────────\n"
            f" • Долг к капиталу D/E: **{avg['debt_to_equity']:.1f}%**\n"
            f" • Ликвидность (Current Ratio): **{avg['current_ratio']:.2f}**\n"
            f" • Чистая маржинальность: **{avg['profit_margin']:.1f}%**\n"
            f" • Рентабельность ROE: **{avg['return_on_equity']:.1f}%**\n"
            f" ───────────────────\n"
            f" • Совокупные дивиденды: **{avg['dividend_yield']:.2f}%**\n"
            f" • Общий свободный кэш компаний: **${avg['free_cash_flow_m']:,.1f}M**\n"
        )

    elif view == "strategies":
        print("🎯 [ПОРТФЕЛЬ]: Рендеринг списка активных стратегий...")
        report_text += "🎯 **Активные стратегии портфеля:**"

        if p_id <= 0 or p_id == 9999:
            report_text += "\n   *Стратегии применимы только к персональному портфелю конкретного брокера.*"
        else:
            inspector = await asyncio.to_thread(PortfolioInspector, db_bot, p_id)
            balances = await asyncio.to_thread(inspector.get_virtual_cash_balances)
            total_capital = float(balances.get("total_capital_usd") or 0.0)
            strat_map = balances.get("strategies", {})

            # Короткие подписи ищутся по system_key -- один запрос на весь список,
            # не по одному на стратегию.
            system_key_by_id = {}
            if strat_map:
                ids_str = ", ".join(str(int(sid)) for sid in strat_map.keys())
                key_rows = await asyncio.to_thread(
                    db_bot.execute_query,
                    f"""
                        SELECT s.id, st.system_key FROM public.strategies s
                        JOIN public.strategy_templates st ON s.template_id = st.id
                        WHERE s.id IN ({ids_str});
                    """
                )
                key_rows = key_rows if isinstance(key_rows, list) else ([key_rows] if key_rows else [])
                system_key_by_id = {int(r["id"]): r.get("system_key") for r in key_rows if r}

            if strat_map:
                for s_id, s in strat_map.items():
                    target_pct = float(s["target_share_pct"])
                    current_usd = float(s["current_holdings_usd"])
                    actual_pct = (current_usd / total_capital * 100.0) if total_capital > 0 else 0.0
                    system_key = system_key_by_id.get(int(s_id))
                    short_name = SHORT_STRATEGY_LABELS.get(system_key, s['strategy_name'])
                    button_text = generate_strategy_button_text(
                        name=short_name,
                        target_pct=target_pct,
                        actual_pct=actual_pct
                    )
                    builder.row(types.InlineKeyboardButton(
                        text=button_text,
                        callback_data=MenuAction(action="view_strategy", strategy_id=int(s_id), portfolio_id=p_id).pack()
                    ))
            else:
                report_text += "\n   *Активные стратегии в этом портфеле не найдены.*"

    # Динамический пульт шторки: переключатель вкладок через общий кубик-сборщик (см. Claude/BACKLOG.md #13)
    tabs = [
        ("📦 Состав портфеля", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets")),
        ("📊 Паспорт качества", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="passport")),
    ]
    if p_id > 0 and p_id != 9999:
        tabs.append(("🎯 Стратегии", MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="strategies")))

    tab_switch_markup = generate_tab_switch_keyboard(tabs, current_sub_view=view)
    for tab_row in tab_switch_markup.inline_keyboard:
        builder.row(*tab_row)

    # Блок навигации назад
    # Вытаскиваем переключатели вкладок портфеля, а нижний блок генерируем через универсальный пульт
    tabs_markup = builder.as_markup()
    
    # Создаем универсальную навигационную клавиатуру
    reply_markup = generate_nav_back_keyboard(
        one_step_back_text="🔙 К общей сводке",
        full_back_callback=MenuAction(action="show_summary").pack()
    )
    
    # Объединяем кнопки вкладок и кнопки универсального возврата UPort
    final_builder = InlineKeyboardBuilder.from_markup(tabs_markup)
    final_builder.attach(InlineKeyboardBuilder.from_markup(reply_markup))

    print("🖥️ [ПОРТФЕЛЬ]: Отправляю собранную шторку в Telegram...")
    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=final_builder.as_markup())        
    except TelegramBadRequest:
        pass
