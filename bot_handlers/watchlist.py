import asyncio
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и навигацию
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
from bot_handlers.summary import get_back_to_menu_keyboard, execute_sql_async

router = Router()

def get_superscript_badge(count: int) -> str:
    """Вспомогательный конвертер чисел в аккуратные суперскрипт-индикаторы."""
    if count <= 0:
        return ""
    superscripts = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
    }
    return "🔔" + "".join(superscripts.get(char, char) for char in str(count)) + " "

@router.callback_query(MenuAction.filter(F.action == "show_watchlist_focus"))
async def process_watchlist_focus_menu(callback: types.CallbackQuery):
    """Шаг 2: Экран выбора фокуса исследования."""
    print(f"\n🔬 [WATCHLIST]: Запрос меню выбора фокуса для user_id = {callback.from_user.id}")
    await callback.answer("Загрузка радаров исследования...")

    sql_portfolios = """
        SELECT p.id, p.name, u.name AS owner_name 
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        WHERE p.id != 9999
        ORDER BY p.id ASC;
    """
    p_res = await execute_sql_async(sql_portfolios)
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

    builder.row(types.InlineKeyboardButton(
        text="🔎 Глобальный интерес (Вне стратегий)",
        callback_data=MenuAction(action="view_watchlist_portfolio", portfolio_id=9999, sub_view="assets").pack()
    ))

    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass

@router.callback_query(MenuAction.filter(F.action == "view_watchlist_portfolio"))
async def process_view_watchlist_portfolio(callback: types.CallbackQuery, callback_data: MenuAction):
    """Шаг 3 и 4: Детализация листов наблюдения (Блок метаданных шапки)."""
    p_id = callback_data.portfolio_id
    view = callback_data.sub_view or "assets"
    
    print(f"\n🔬 [WATCHLIST]: Сборка шторки для p_id = {p_id}, вкладка = '{view}'")
    await callback.answer("Сборка паспорта исследования...")

    if p_id == 9999:
        owner_name = "Семья"
        portfolio_name = "Глобальный интерес"
        strategy_type = "Вне стратегий (Общая песочница)"
    else:
        p_sql = f"""
            SELECT p.name AS portfolio_name, u.name AS owner_name, p.strategy_type
            FROM public.portfolios p
            JOIN public.users u ON p.owner_id = u.id
            WHERE p.id = {p_id};
        """
        p_meta_res = db_bot.execute_query(p_sql)
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

    if p_id == 9999:
        report_text = f"🔎 **{portfolio_name.upper()}**\n"
    else:
        report_text = f"🔬 **ФОКУС НА {portfolio_name.upper()}**\n"
        
    report_text += f"👤 {owner_name}\n"
    report_text += f"💼 `{strategy_type}`\n"
    report_text += f"───────\n"

    builder = InlineKeyboardBuilder()

    # 3. ЛОГИКА ОТРЕНДЕРИВАНИЯ ВНУТРЕННОСТЕЙ ШТОРОК
    if view == "assets":
        report_text += "🎯 **АКТИВНЫЕ РАДАРЫ И ЦЕЛИ СЛЕДОВАНИЯ:**"
        
        # 🔥 СУПЕР-ЗАПРОС АГРЕГАЦИИ С ВЫГРУЗКОЙ ТЕКУЩЕЙ ЦЕНЫ БРОКЕРА (last_price)
        if p_id == 9999:
            watchlist_query = """
                SELECT w.id, l.id AS listing_id, l.broker_symbol, t.symbol, t.company_name, l.last_price,
                       w.considered_at, w.watched_at, w.ordered_at, w.bought_at, w.sold_out_at,
                       COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
                FROM public.watchlist w
                JOIN public.listings l ON w.listing_id = l.id
                JOIN public.tickers t ON l.ticker_id = t.id
                LEFT JOIN public.alerts al ON al.listing_id = l.id AND al.is_active = true
                WHERE w.portfolio_id = 9999
                GROUP BY w.id, l.id, t.symbol, t.company_name, l.last_price
                ORDER BY t.symbol ASC;
            """
        else:
            watchlist_query = f"""
                SELECT w.id, l.id AS listing_id, l.broker_symbol, t.symbol, t.company_name, l.last_price,
                       w.considered_at, w.watched_at, w.ordered_at, w.bought_at, w.sold_out_at,
                       COUNT(CASE WHEN al.is_active = true THEN 1 END)::int as active_alerts_count
                FROM public.watchlist w
                JOIN public.listings l ON w.listing_id = l.id
                JOIN public.tickers t ON l.ticker_id = t.id
                LEFT JOIN public.alerts al ON al.listing_id = w.listing_id 
                                          AND al.portfolio_id = w.portfolio_id 
                                          AND al.is_active = true
                WHERE w.portfolio_id = {p_id}
                GROUP BY w.id, l.id, t.symbol, t.company_name, l.last_price
                ORDER BY t.symbol ASC;
            """
            
        w_res_raw = db_bot.execute_query(watchlist_query)
        w_res = w_res_raw if isinstance(w_res_raw, list) else ([w_res_raw] if w_res_raw else [])

        if w_res:
            for item in w_res:
                pure_symbol = item['symbol']
                l_id = int(item['listing_id'] or 0)
                last_price = float(item['last_price'] or 0)
                
                # 🛠️ НОВЫЙ ДИЗАЙН-КОД: Вычисляем иконку фазы жизненного цикла UPort
                if item.get('sold_out_at') is not None:
                    crystal = "🏁"
                elif item.get('bought_at') is not None:
                    crystal = "💼"
                elif item.get('ordered_at') is not None:
                    crystal = "📃" # Заменяем старую иконку на аккуратный биржевый свиток приказа
                elif item.get('watched_at') is not None:
                    crystal = "🎯"
                elif item.get('considered_at') is not None:
                    crystal = "🔍"
                else:
                    crystal = "🔹"
                
                # 🔥 ВНЕДРЕНИЕ СУПЕРСКРИПТ-БЭДЖА АЛЕРТОВ (ВСТАЕТ МЕЖДУ ИКОНКОЙ И ТИКЕРOM)
                alerts_count = int(item.get('active_alerts_count') or 0)
                alert_badge = get_superscript_badge(alerts_count)
                
                # Собираем премиальный текст кнопки по новому ТЗ (Иконка + Колокольчик + Тикер + Цена)
                button_text = f"{crystal} {alert_badge}{pure_symbol} • ${last_price:,.2f}"
                
                builder.row(types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, ticker_name=pure_symbol, sub_view="alerts").pack()
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
        
    builder.row(types.InlineKeyboardButton(text="🔙 К выбору фокуса", callback_data=MenuAction(action="show_watchlist_focus").pack()))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    print("🖥️ [WATCHLIST]: Отправляю обновленную шторку в Telegram...")
    try:
        await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest:
        pass
