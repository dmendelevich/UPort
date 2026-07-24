import asyncio
import logging
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты СУБД, фабрику и клавиатуры из доноров
from database import db_bot, db_sys
from bot_handlers.common import MenuAction
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
        
        # Формируем стерильную шапку карточки UPort
        report_text = f"🌟 ***  {pure_symbol}  ***\n"
        report_text += f"{l_row['company_name'] if l_row else 'Инструмент UPort'}\n"
        report_text += f"💵 Текущая цена рынка: **${last_price:,.2f}**\n\n"
        report_text += f"🎯 **СПИСОК АЛЕРТОВ И ТРИГГЕРОВ:**\n"
        
        if alerts_list:
            for idx, al in enumerate(alerts_list, 1):
                # 1. Определяем контур и владельца стратегии счета
                c_icon = "📡 [FB]" if al['source_type'] == 'broker' else "🧠 [UP]"
                p_name = al['portfolio_name'] or f"П{p_id}"
                u_owner = al['portfolio_owner_name'] or "Инвестор"
                
                report_text += f"\n**{idx}. {c_icon} • Портфель {p_name} ({u_owner})**\n"
                
                # 2. Форматируем условие цены на основе нашей новой 3NF-структуры колонок
                if al['trigger_price_min'] is not None and al['trigger_price_max'] is not None:
                    report_text += f" • Канал цен: **${float(al['trigger_price_min']):,.2f} - ${float(al['trigger_price_max']):,.2f}**\n"
                elif al['trigger_pct'] is not None:
                    report_text += f" • Динамический триггер: **{al['condition_type']}{float(al['trigger_pct'])}%**\n"
                else:
                    report_text += f" • Цена: **{al['condition_type']} ${float(al['trigger_price'] or 0):,.2f}**\n"
                
                # 3. Выводим статус и честное время срабатывания UPort
                if al['is_active']:
                    report_text += f" • ⏳ Ожидание триггера\n"
                else:
                    t_time = ""
                    if al['triggered_at']:
                        # Красиво форматируем timestamp в читаемую дату/время
                        if isinstance(al['triggered_at'], str):
                            t_time = " " + al['triggered_at'].split('.')[0].replace('T', ' ')
                        else:
                            t_time = " " + al['triggered_at'].strftime('%Y-%m-%d %H:%M:%S')
                    report_text += f" • 🔥 **СРАБОТАЛ**{t_time}\n"
                    
                # 4. Выводим создателя (Инициатора) алерта
                if al['ai_strategy_id'] is not None:
                    report_text += f" • Установил: 🤖 ИИ (Стратегия #{al['ai_strategy_id']})\n"
                else:
                    creator = al['creator_name'] or u_owner
                    report_text += f" • Установил: 👤 {creator}\n"
                    
                # 5. Выводим ручную текстовую заметку инвестора
                if al['note']:
                    report_text += f" • Заметка: *{al['note']}*\n"
        else:
            report_text += "   *Активные и сработавшие алерты по данной бумаге в СУБД отсутствуют.*\n"
            
        report_text += f"───────\n"
        
        # Строим пульт смарт-навигации для возврата строго в радары исследования
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="🔙 Назад к радарам слежения", 
            callback_data=MenuAction(action="view_watchlist_portfolio", portfolio_id=p_id, sub_view="assets").pack()
        ))
        builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
        
        print("🖥️ [ТИКЕР АЛЕРТЫ]: Отправляю изолированную шторку в Telegram...")
        try:
            await callback.message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        except TelegramBadRequest:
            pass
        return # 🚨 ПРЕДОХРАНИТЕЛЬ: Выходим из метода, полностью блокируя выполнение старого кода кошелька!
    # =========================================================================

    # 3. ФОРМИРОВАНИЕ УЛЬТИМАТИВНЫХ ШАПОК СТУДИИ ДИЗАЙНА UPORT
    if is_owner_view:
        # Вариант 1: В портфеле конкретного счета
        owner_row = next((h for h in all_holders if h['portfolio_id'] == p_id), None)
        p_name = owner_row['portfolio_name'] if owner_row else f"П{p_id}"
        u_name = owner_row['owner_name'] if owner_row else "User"
        
        text = f"🌟 ***  {pure_symbol}  ***\n"
        text += f"{l_row['company_name']}\n"
        text += f"В ПОРТФЕЛЕ {p_name} ({u_name})\n"
        text += f"───────\n"
        text += f"💵 Текущая цена: **{sign}{last_price:,.2f}**\n"
        
        if owner_row:
            qty = float(owner_row['quantity'])
            avg_price = float(owner_row['avg_price'] or 0)
            cost_basis = qty * avg_price
            market_val = qty * last_price
            profit = market_val - cost_basis
            profit_pct = (profit / cost_basis * 100) if cost_basis > 0 else 0.0
            p_sign = "+" if profit >= 0 else ""
            clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"
            
            text += f"📊 Позиция: **{clean_qty} шт.** • Средняя: **{sign}{avg_price:,.2f}**\n"
            text += f"📈 Результат: **{p_sign}{sign}{profit:,.2f} ({p_sign}{profit_pct:.1f}%)**\n"
            text += f"⏱️ Владение: **{owner_row['holding_days']} дней**\n"

    elif is_family_view:
        # Вариант 2: В Сводном семейном капитале
        text = f"🌟 ***  {pure_symbol}  ***\n"
        text += f"{l_row['company_name']}\n"
        text += f"В ОБЩЕМ КАПИТАЛЕ СЕМЬИ\n"
        text += f"───────\n"
        text += f"💵 Текущая цена рынка: **{sign}{last_price:,.2f}**\n\n"
        text += f"👥 **Распределение по счетам семьи:**\n"
        
        total_family_qty = 0.0
        total_family_cost = 0.0
        total_family_market = 0.0
        
        if all_holders:
            for h in all_holders:
                h_qty = float(h['quantity'])
                h_avg = float(h['avg_price'] or 0)
                h_basis = h_qty * h_avg
                h_market = h_qty * last_price
                h_profit = h_market - h_basis
                h_profit_pct = (h_profit / h_basis * 100) if h_basis > 0 else 0.0
                h_sign = "+" if h_profit >= 0 else ""
                h_clean_qty = int(h_qty) if h_qty.is_integer() else f"{h_qty:.2f}"
                
                text += f" • {h['owner_name']} ({h['portfolio_name']}) — **{h_clean_qty} шт.** • Результат: {h_sign}{sign}{h_profit:,.2f} ({h_sign}{h_profit_pct:.1f}%)\n"
                
                total_family_qty += h_qty
                total_family_cost += h_basis
                total_family_market += h_market
            
            tf_profit = total_family_market - total_family_cost
            tf_profit_pct = (tf_profit / total_family_cost * 100) if total_family_cost > 0 else 0.0
            tf_sign = "+" if tf_profit >= 0 else ""
            tf_clean_qty = int(total_family_qty) if total_family_qty.is_integer() else f"{total_family_qty:.2f}"
            
            text += f"───────\n"
            text += f"📊 **СОВОКУПНЫЙ КАПИТАЛ КЛАНА:**\n"
            text += f"• Всего в удержании: **{tf_clean_qty} шт.**\n"
            text += f"• Общая оценка: **{sign}{total_family_market:,.2f}**\n"
            text += f"• Чистая прибыль клана: **{tf_sign}{sign}{tf_profit:,.2f} ({tf_sign}{tf_profit_pct:.1f}%)**\n"
        else:
            text += "   *Ни один член семьи пока не владеет данным активом.*\n"

    elif is_research_portfolio:
        # Вариант 4: Исследование В ПОРТФЕЛЕ (Фокус конкретной стратегии)
        text = f"🌟 ***  {pure_symbol}  ***\n"
        text += f"{l_row['company_name']}\n"
        text += f"ИССЛЕДОВАНИЕ ПО СТРАТЕГИИ\n"
        text += f"───────\n"
        text += f"💵 Текущая цена: **{sign}{last_price:,.2f}**\n"
        text += f"🎯 Статус: Наблюдение по декларации счета\n"
    else:
        # Вариант 3: Исследование ВООБЩЕ (Глобальная песочница 9999)
        text = f"🌟 ***  {pure_symbol}  ***\n"
        text += f"{l_row['company_name'] if 'company_name' in l_row else 'Unknown'}\n"
        text += f"ГЛОБАЛЬНОЕ ИССЛЕДОВАНИЕ СИСТЕМЫ\n"
        text += f"───────\n"
        text += f"💵 Текущая цена: **{sign}{last_price:,.2f}**\n"
        text += f"🎯 Статус: В списке глобального наблюдения (Watchlist)\n\n"
        
        if all_holders:
            text += f"👥 **Семейный кросс-радар:**\n"
            for h in all_holders:
                h_clean_qty = int(h['quantity']) if float(h['quantity']).is_integer() else f"{h['quantity']:.2f}"
                text += f" • Актив куплен у: **{h['owner_name']}** ({h_clean_qty} шт. в портфеле {h['portfolio_name']})\n"

    # 4. ВЫЗОВ РИСК-АУДИТОРА IPS (Только для личного портфеля или портфельного фокуса)
    if is_owner_view or is_research_portfolio:
        audit_id = p_id if is_owner_view else 1
        # 🔥 РЕФАКТОРИНГ: Заменяем на чистый execute_row
        p_row = await asyncio.to_thread(db_bot.execute_row, f"SELECT name FROM public.portfolios WHERE id = {audit_id};")
        p_title = p_row.get('name', f"Счет #{audit_id}")

        warnings_list = audit_ticker_for_portfolio(broker_symbol if l_id > 0 else pure_symbol, audit_id, db_bot)
        text += f"───────\n🛡️ **Риск-аудит IPS {p_title}:**\n"
        if warnings_list:
            for w in warnings_list:
                text += f" {w}\n"
        else:
            text += " ✅ Риск-декларация: лимиты веса и налоговой нагрузки соблюдены.\n"

    text += f"───────\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="💼 Владение и Ордера", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, ticker_name=pure_symbol, sub_view="owner", strategy_id=strategy_id).pack()),
        types.InlineKeyboardButton(text="📊 Метрики Yahoo", callback_data=MenuAction(action="view_ticker", portfolio_id=p_id, listing_id=l_id, ticker_name=pure_symbol, sub_view="yahoo", strategy_id=strategy_id).pack())
    )

    # 5. ЛОГИКА РАЗВОДКИ ВНУТРЕННОСТЕЙ ШТОРОК
    if view == "yahoo":
        print("📊 [ТИКЕР]: Выгружаю фундаментальные коэффициенты из public.tickers...")
        # 🔥 РЕФАКТОРИНГ: Точечно переводим на безопасное чтение одной строки
        t = await asyncio.to_thread(db_bot.execute_row, f"SELECT * FROM public.tickers WHERE id = {t_id};")

        if t and (t.get('pe_trailing') is not None or t.get('debt_to_equity') is not None):
            fcf_val = float(t['free_cash_flow'] or 0) / 1_000_000
            text += (
                f"📊 **Аналитические мультипликаторы бизнеса:**\n"
                f" • Окупаемость P/E: **{float(t.get('pe_trailing') or 0):.1f}** | Прогноз P/E: **{float(t.get('pe_forward') or 0):.1f}**\n"
                f" • Коэффициент PEG: **{float(t.get('peg_ratio') or 0):.2f}**\n"
                f" • Цена к выручке P/S: **{float(t.get('price_to_sales') or 0):.1f}** | К балансу P/B: **{float(t.get('price_to_book') or 0):.1f}**\n"
                f" • Стоимость бизнеса EV/EBITDA: **{float(t.get('ev_to_ebitda') or 0):.1f}**\n"
                f" ───────\n"
                f" • Долг к капиталу D/E: **{float(t.get('debt_to_equity') or 0):.1f}%**\n"
                f" • Ликвидность (Current Ratio): **{float(t.get('current_ratio') or 0):.2f}**\n"
                f" • Чистая маржинальность: **{float(t.get('profit_margin') or 0):.1f}%**\n"
                f" • Рентабельность ROE: **{float(t.get('return_on_equity') or 0):.1f}%**\n"
                f" ───────\n"
                f" • Див. доходность: **{float(t.get('dividend_yield') or 0):.2f}%** | Выплаты (Payout): **{float(t.get('payout_ratio') or 0):.1f}%**\n"
                f" • Свободный кэш (FCF): **${fcf_val:,.1f}M**\n"
                f" ───────\n"
                f" • Комиссия ETF (Expense Ratio): **{float(t.get('expense_ratio') or 0)*100:.2f}%**\n"
                f" • Сектор: **{t.get('sector', 'N/A')}** | Индустрия: **{t.get('industry', 'N/A')}**\n"
            )
        else:
            text += "📊 **Фундаментальные показатели бизнеса:**\n   *Для данного типа актива экономические мультипликаторы стоимости отсутствуют.*"
    else:
        # Вкладка Владение и Ордера (По умолчанию)
        print("⏳ [ТИКЕР]: Выгружаю активные биржевые приказы из академической public.orders...")
        
        # Человеческий джойн пользователей СУБД и расчет возраста ордеров в днях
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
            text += "⏳ **Активные приказы этого портфеля:**\n"
            current_orders = [o for o in live_orders_res if o['portfolio_name'] == p_name]
            if current_orders:
                # Наше ТЗ: разделяем открытые заявки на изменение позы и защитные стоп-механизмы
                market_orders = [o for o in current_orders if int(o['type']) not in (5, 6)]
                trigger_orders = [o for o in current_orders if int(o['type']) in (5, 6)]
                
                if market_orders:
                    text += " 🛒 **ОТКРЫТЫЕ ЗАЯВКИ (Лимитные/Рыночные):**\n"
                    for o in market_orders:
                        op_label = "ПОКУПКА" if int(o['oper']) in (1, 2) else "ПРОДАЖА"
                        text += f"  • 🔹 ЛИМИТНАЯ {op_label} ➡️ {float(o['q']):.0f} шт. по цене {o['sign']}{float(o['p'] or 0):,.2f} ({o['order_age_days']} дн. назад)\n"
                
                if trigger_orders:
                    text += " 🛡️ **ЗАЩИТА И ЦЕЛИ (Стоп-ордера):**\n"
                    for o in trigger_orders:
                        label = "СТОП-ЛОСС" if int(o['type']) == 5 else "ТЕЙК-ПРОФИТ"
                        emoji = "🛑" if int(o['type']) == 5 else "🎯"
                        text += f"  • {emoji} {label} (По рынку) ➡️ {float(o['q']):.0f} шт. • Активация: {o['sign']}{float(o['stop_price'] or o['p']):,.2f} ({o['order_age_days']} дн. назад)\n"
            else:
                text += "   *Активных приказов по данной бумаге на бирже нет.*\n"

            builder.row(types.InlineKeyboardButton(
                text="🔗 Привязать ордер к плану",
                callback_data=MenuAction(action="pipeline_link_start", portfolio_id=p_id, ticker_id=t_id, listing_id=l_id).pack()
            ))
        else:
            # Для Интегральной или Исследовательской карточки группируем СТРОГО по портфелям владельцев
            text += "⏳ **Совокупные активные приказы семьи:**\n"
            if live_orders_res:
                distinct_owners = sorted(list(set(o['owner_name'] for o in live_orders_res)))
                for owner in distinct_owners:
                    owner_orders = [o for o in live_orders_res if o['owner_name'] == owner]
                    p_title_str = owner_orders[0]['portfolio_name'] if owner_orders else ""
                    text += f" 👤 **{owner} ({p_title_str}):**\n"
                    for o in owner_orders:
                        if int(o['type']) == 5:
                            text += f"  • 🛑 СТОП-ЛОСС ➡️ {float(o['q']):.0f} шт. • Активация: {o['sign']}{float(o['stop_price'] or o['p']):,.2f} ({o['order_age_days']} дн. назад)\n"
                        elif int(o['type']) == 6:
                            text += f"  • 🎯 ТЕЙК-ПРОФИТ ➡️ {float(o['q']):.0f} шт. • Активация: {o['sign']}{float(o['stop_price'] or o['p']):,.2f} ({o['order_age_days']} дн. назад)\n"
                        else:
                            op_lbl = "ПОКУПКА" if int(o['oper']) in (1, 2) else "ПРОДАЖА"
                            text += f"  • 🔹 ЛИМИТНАЯ {op_lbl} ➡️ {float(o['q']):.0f} шт. по цене {o['sign']}{float(o['p'] or 0):,.2f} ({o['order_age_days']} дн. назад)\n"
            else:
                text += "   *Активных приказов у членов семьи на бирже нет.*\n"

        # =========================================================================
        # 🔥 ВНЕДРЕНИЕ КОНТУРА АЛЕРТОВ НА ГЛАВНУЮ СТРАНИЦУ КАРТОЧКИ ТИКЕРА (ДЛЯ КОШЕЛЬКА)
        # =========================================================================
        text += "\n🎯 **Взведенные алерты и триггеры по бумаге:**\n"
        
        # Вытаскиваем алерты из нашего нового реляционного метода в database.py
        alerts_list = db_bot.get_ticker_alerts_context(l_id)
        
        if alerts_list:
            for idx, al in enumerate(alerts_list, 1):
                c_icon = "📡 [FB]" if al['source_type'] == 'broker' else "🧠 [UP]"
                
                # Фильтруем алерты для вывода: 
                # На личном экране (p_id > 0) показываем только алерты этого портфеля.
                # На сводном экране (p_id == 0) — выводим алерты вообще всей семьи.
                if p_id != 0 and al.get('portfolio_id') != p_id:
                    continue
                    
                p_name = al['portfolio_name'] or f"П{p_id}"
                
                if al['trigger_price_min'] is not None and al['trigger_price_max'] is not None:
                    c_condition = f"Канал цен: ${float(al['trigger_price_min']):,.2f} - ${float(al['trigger_price_max']):,.2f}"
                elif al['trigger_pct'] is not None:
                    c_condition = f"Динамический триггер: {al['condition_type']}{float(al['trigger_pct'])}%"
                else:
                    c_condition = f"Цена {al['condition_type']} ${float(al['trigger_price'] or 0):,.2f}"
                
                if al['is_active']:
                    c_status = "⏳ Ожидание"
                else:
                    t_time = ""
                    if al['triggered_at']:
                        if isinstance(al['triggered_at'], str):
                            t_time = " " + al['triggered_at'].split('.').replace('T', ' ')
                        else:
                            t_time = " " + al['triggered_at'].strftime('%Y-%m-%d %H:%M:%S')
                    c_status = f"🔥 **СРАБОТАЛ**{t_time}"
                    
                if al['ai_strategy_id'] is not None:
                    creator = f"🤖 ИИ (Стратегия #{al['ai_strategy_id']})"
                else:
                    creator = f"👤 {al['creator_name'] or al['portfolio_owner_name'] or 'Инвестор'}"
                    
                text += f" {idx}. {c_icon} ({p_name}) • {c_condition} • {c_status} • Установил: {creator}\n"
                
                if al['note']:
                    text += f"     └ *Заметка: {al['note']}*\n"
        else:
            text += "   *Активные или сработавшие алерты в СУБД отсутствуют.*\n"
        # =========================================================================


    # Смарт-навигация назад в зависимости от точки входа инвестора
    if strategy_id > 0:
        builder.row(types.InlineKeyboardButton(text="🔙 К стратегии", callback_data=MenuAction(action="view_strategy", strategy_id=strategy_id, portfolio_id=p_id).pack()))
    elif is_owner_view:
        builder.row(types.InlineKeyboardButton(text="🔙 К списку активов", callback_data=MenuAction(action="view_portfolio", portfolio_id=p_id, sub_view="assets").pack()))
    elif is_family_view:
        builder.row(types.InlineKeyboardButton(text="🔙 К сводке капитала", callback_data=MenuAction(action="view_portfolio", portfolio_id=0, sub_view="assets").pack()))
    else:
        builder.row(types.InlineKeyboardButton(text="🔙 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
        
    # Системная кнопка безусловного сброса в корень UPort
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    print("🖥️ [ТИКЕР]: Отправляю карточку акции в Telegram...")

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except TelegramBadRequest as e:
        print(f"⚠️ Ошибка редактирования шторки тикера: {e}")
        pass
