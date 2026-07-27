import logging
import asyncio
import yfinance as yf
from database import db_bot
from analytics.analytics_utils import convert_currency_amount, convert_to_base_currency
from analytics.portfolio_inspector import PortfolioInspector

# ─── СТАНДАРТ ШИРИНЫ ЭКРАНОВ (header/body/footer) -- см. Claude/05_strategy_screen_and_kubiki.md,
# BACKLOG.md #19. Используется, чтобы решить, влезает ли строка целиком, или её нужно красиво
# перенести (wrap_screen_line) -- не общесистемный параметр, поэтому живёт здесь, а не в config.py.
# Калибровано по реальному экрану iPhone 12 (2026-07-25, обычный размер шрифта, без Bold Text) --
# пользователь замерил живьём 22-24 символа на строку до переноса. Взято 22 (нижняя граница).
SCREEN_WIDTH_CHARS = 22

# Разделительная черта -- декоративный элемент. Символ "─" (box-drawing) визуально шире обычной
# буквы в этом шрифте -- даже 15 таких символов переносились на второй ряд на реальном экране
# (см. скриншот 2026-07-25), поэтому короче SCREEN_WIDTH_CHARS ощутимо сильнее. Одна на все экраны.
SEPARATOR_WIDTH_CHARS = 12
SEPARATOR_LINE = "─" * SEPARATOR_WIDTH_CHARS


def _visible_length(text: str) -> int:
    """Длина строки без markdown-разметки (**жирный**/*курсив*/_курсив_) -- она не видна как символы."""
    return len(text.replace("**", "").replace("*", "").replace("_", ""))


def justify_line(left: str, right: str, width: int = SCREEN_WIDTH_CHARS) -> str:
    """
    Прижимает left к левому краю строки, right -- к правому, дополняя пробелами до width
    знакомест по ВИДИМОЙ длине (без markdown-разметки). Если left+right сами по себе не
    помещаются -- через один пробел, без принудительного растягивания. Не даёт пиксельно
    точного края (шрифт не моноширинный, см. обсуждение 2026-07-25), но выравнивает по
    числу символов, чего для карточек UPort достаточно.
    """
    pad = width - _visible_length(left) - _visible_length(right)
    if pad < 1:
        return f"{left} {right}"
    return f"{left}{' ' * pad}{right}"


def wrap_screen_line(prefix: str, content: str, width: int = SCREEN_WIDTH_CHARS) -> str:
    """
    Переносит строку экрана, если prefix+content не помещаются в width знакомест.
    Для ОДНОРОДНОГО текста (не набора самостоятельных параметров) -- жадно заполняет
    первую строку по словам, продолжение переносится с висячим отступом длиной в
    prefix (текст визуально начинается из той же колонки, где начался контент после
    иконки). Если разрыв случайно пришёлся сразу после "—", он дублируется в начале
    следующей строки. Для строк из нескольких параметров, соединённых разделителем
    (например, "Сектор — Индустрия") -- см. wrap_param_line ниже, там другая логика:
    параметры не рвутся по словам между собой.
    """
    full = f"{prefix}{content}"
    if len(full) <= width:
        return full

    avail = max(1, width - len(prefix))
    indent = " " * len(prefix)
    words = content.split(" ")

    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= avail:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    out = [f"{prefix}{lines[0]}"]
    for line in lines[1:]:
        connector = "— " if out[-1].rstrip().endswith("—") and not line.startswith("—") else ""
        out.append(f"{indent}{connector}{line}")
    return "\n".join(out)


def wrap_param_line(prefix: str, params: list, separator: str = " — ", width: int = SCREEN_WIDTH_CHARS) -> str:
    """
    Строка из нескольких самостоятельных параметров, соединённых разделителем
    (например, "Сектор — Индустрия"). В отличие от wrap_screen_line, параметры не
    рвутся по словам между собой: если всё не помещается в width, первый параметр
    остаётся на основной строке (с разделителем в конце), каждый следующий -- на
    своей строке, прижатой ВПРАВО так, чтобы последний символ оказался ровно на
    width-м знакоместе, с разделителем перед ним. Параметр, который сам по себе не
    помещается в width -- переносится по словам (wrap_screen_line), с тем же
    прижатым разделителем в роли префикса.
    """
    dash = separator.strip()
    full = f"{prefix}{separator.join(params)}"
    if len(full) <= width:
        return full

    lines = [f"{prefix}{params[0]} {dash}"]
    for param in params[1:]:
        continuation = f"{dash} {param}"
        if len(continuation) <= width:
            lines.append(continuation.rjust(width))
        else:
            lines.append(wrap_screen_line(f"{dash} ", param, width=width))
    return "\n".join(lines)


async def format_premium_header(ticker_id: int, portfolio_id: int = 0, target_currency: str = "USD", target_sign: str = "$") -> str:
    """
    Универсальный сборщик премиальной шапки тикера UPort (общий header/body/footer
    стандарт -- см. Claude/05_strategy_screen_and_kubiki.md, BACKLOG.md #19, #30).

    Цена берётся из public.listings, если бумага уже легализована у какого-то брокера
    (предпочтительно -- у брокера portfolio_id, если он есть; иначе любой существующий
    листинг); если листинга ещё нет вообще (бумага только что найдена глобальным
    поиском, ещё не добавлена в список наблюдения) -- цена берётся напрямую с Yahoo
    Finance. Источник цены (короткое имя брокера или "YF") подписан явно, чтобы не
    путать слои (см. BACKLOG.md #21 -- смешение канонической цены tickers и брокерской
    цены listings уже давало реальный баг).

    Мультивалютность (BACKLOG.md #30): итоговая цена выводится в target_currency
    (валюта СМОТРЯЩЕГО пользователя, считается один раз за рендер вызывающим кодом --
    см. tickers.py). Заодно починен живой баг: currencies.multiplier раньше применялся
    НАОБОРОТ -- к цене из listings (брокерский слой, уже в настоящих единицах, см.
    Claude/07_glossary.md), а не к цене из Yahoo (канонический слой tickers, нативные
    единицы биржи -- например, пенсы для LSE). target_currency="USD" по умолчанию --
    старое поведение для вызывающего кода, который ещё не прокидывает валюту смотрящего
    (ticker_search.py, strategies.py, strategy_resolver.py -- вне текущего ретрофита).
    """
    try:
        # 1. Запрашиваем паспорт инструмента
        sql_t = f"""
            SELECT t.symbol, t.company_name, t.asset_type, t.sector, t.industry,
                   t.currency_id, t.ticker_name_map, e.exchange_code
            FROM public.tickers t
            LEFT JOIN public.exchanges e ON t.exchange_mic = e.mic
            WHERE t.id = {int(ticker_id)} LIMIT 1;
        """
        t = await asyncio.to_thread(db_bot.execute_row, sql_t)

        if not t:
            return "🌟 **Инструмент UPort**\n❌ Паспорт бумаги не найден в СУБД."

        display_symbol = t.get("symbol", "N/A")

        # 2. Ищем уже существующий листинг у любого брокера -- предпочтительно у брокера
        # переданного портфеля, если он есть. Если листинга нет вообще -- бумага ещё
        # нигде не легализована (не в watchlist ни у кого), это нормально.
        sql_l = f"""
            SELECT l.last_price, l.currency_id, b.short_name AS broker_short_name
            FROM public.listings l
            JOIN public.brokers b ON b.id = l.broker_id
            LEFT JOIN public.portfolios p ON p.id = {int(portfolio_id)} AND p.broker_id = l.broker_id
            WHERE l.ticker_id = {int(ticker_id)}
            ORDER BY (p.id IS NOT NULL) DESC
            LIMIT 1;
        """
        l_row = await asyncio.to_thread(db_bot.execute_row, sql_l)

        if l_row:
            # Цена из брокерского листинга -- брокерский слой, уже в НАСТОЯЩИХ единицах
            # (см. Claude/07_glossary.md) -- multiplier тут не нужен, только курс FX
            raw_price = float(l_row.get("last_price") or 0.0)
            curr_id = l_row.get("currency_id", "USD")
            live_price = await asyncio.to_thread(convert_currency_amount, db_bot, raw_price, curr_id, target_currency)
            price_source = l_row.get("broker_short_name") or "брокер"
        else:
            # Листинга ещё нет нигде -- берём цену напрямую с Yahoo Finance, канонический
            # тикер/валюта -- из public.tickers, нативные единицы биржи (см.
            # Claude/07_glossary.md) -- ЗДЕСЬ multiplier нужен, раньше не применялся вообще
            ticker_name_map = t.get("ticker_name_map") or {}
            yahoo_symbol = ticker_name_map.get("YAHOO", display_symbol)
            try:
                fast_info = await asyncio.to_thread(lambda: yf.Ticker(yahoo_symbol).fast_info)
                raw_price = float(fast_info.get("last_price") or fast_info.get("open") or 0.0)
            except Exception as yf_err:
                logging.warning(f"⚠️ [BOT SCREENS]: Не удалось получить живую цену Yahoo для {yahoo_symbol}: {yf_err}")
                raw_price = 0.0
            curr_id = t.get("currency_id") or "USD"
            live_price = await asyncio.to_thread(convert_to_base_currency, db_bot, raw_price, curr_id, target_currency)
            price_source = "YF"

        # 3. Формируем остальные текстовые маркеры дизайна
        asset_type = str(t.get('asset_type', 'EQUITY')).upper().strip()
        type_badge = "Фонд (ETF)" if asset_type == 'ETF' else "Акция"
        exch_code = t.get("exchange_code") or "GLOBAL"

        company_line = wrap_screen_line("🏢 ", t.get('company_name', 'Unknown'))
        sector_line = wrap_param_line("📦 ", [t.get('sector', 'N/A'), t.get('industry', 'N/A')])
        badge_line = wrap_screen_line("🏷️ ", f"{type_badge}, {exch_code}")
        symbol_price_line = justify_line(
            f"🌟 **{display_symbol}**",
            f"💵 {price_source}: **{target_sign}{live_price:,.2f}**"
        )

        header = (
            f"{symbol_price_line}\n"
            f"{company_line}\n"
            f"{sector_line}\n"
            f"{badge_line}\n"
            f"{SEPARATOR_LINE}\n"
        )
        return header
    except Exception as e:
        logging.error(f"🚨 [BOT SCREENS ERROR]: Сбой сборщика шапки id={ticker_id}: {e}")
        return "🌟 **Инструмент UPort**\n⚠️ Ошибка сбора паспортных данных."

# Предельно короткие подписи стратегий для тесной строки-заголовка Блока 1
# (полное имя strategy_name может быть переименовано пользователем и слишком длинное
# для одной строки с объёмом рядом -- см. обсуждение 2026-07-26). Ключ -- system_key
# (стабильный), как и в bot_handlers/portfolios.py::SHORT_STRATEGY_LABELS (там подписи
# менее агрессивно урезаны -- для кнопок с большей шириной, здесь -- для узкой строки).
STRATEGY_MICRO_LABELS = {
    "REVOLVER": "Револьвер.",
    "CONSERVATIVE_ACCUMULATION": "Консерв.",
    "TREND_FOLLOWING": "Тренд.",
    "CASH_RESERVE": "Кэш/Резерв",
    "UNALLOCATED": "Неопред.",
}


async def format_position_financials(portfolio_id: int, listing_id: int, last_price: float, sign: str, strategy_id: int = 0, fx_rate: float = 1.0) -> str:
    """
    Блок 1 стандарта body тикера (см. Claude/05_strategy_screen_and_kubiki.md): финансовая
    характеристика позиции -- количество, цена входа vs баланс, вложено vs сейчас,
    прибыль, срок владения, годовая доходность. Несёт собственную подпись владения
    (портфель или стратегия) -- заменяет отдельную строку-контекст, от которой решили
    отказаться (обсуждение 2026-07-26): подпись естественно принадлежит этому блоку,
    а не отдельной строке над всеми блоками.

    strategy_id=0 -> считаем по всему портфелю (v_assets_full.quantity), strategy_id>0
    -> по доле, закреплённой за стратегией (v_strategy_assets_full.allocated_quantity).
    Цена входа (avg_price) в обоих случаях ОДНА И ТА ЖЕ портфельная -- у strategy_assets
    нет собственной цены входа, это осознанное упрощение модели данных, не баг.

    Мультивалютность (BACKLOG.md #30): last_price/sign вызывающий код (tickers.py) уже
    передаёт в валюте СМОТРЯЩЕГО пользователя; fx_rate -- тот же курс (нативная валюта
    листинга -> валюта смотрящего), посчитанный один раз за рендер, применяется здесь
    только к avg_price (единственная сумма, которую эта функция достаёт из БД сама).
    """
    if strategy_id > 0:
        sql = f"""
            SELECT vsaf.allocated_quantity AS quantity, vsaf.avg_price, vsaf.portfolio_name, st.system_key,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(vsaf.position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
            FROM public.v_strategy_assets_full vsaf
            JOIN public.strategies s ON s.id = vsaf.strategy_id
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE vsaf.strategy_id = {int(strategy_id)} AND vsaf.listing_id = {int(listing_id)}
            LIMIT 1;
        """
    else:
        sql = f"""
            SELECT quantity, avg_price, portfolio_name,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - COALESCE(position_opened_at, CURRENT_TIMESTAMP)))::int AS holding_days
            FROM public.v_assets_full
            WHERE portfolio_id = {int(portfolio_id)} AND listing_id = {int(listing_id)} AND quantity > 0
            LIMIT 1;
        """

    row = await asyncio.to_thread(db_bot.execute_row, sql)
    if not row:
        return "❌ Позиция по этой бумаге не найдена.\n"

    qty = float(row["quantity"] or 0)
    avg_price = float(row["avg_price"] or 0) * fx_rate
    holding_days = int(row.get("holding_days") or 0)

    cost_basis = qty * avg_price
    market_val = qty * last_price
    profit = market_val - cost_basis
    profit_pct = (profit / cost_basis * 100) if cost_basis > 0 else 0.0
    p_sign = "+" if profit >= 0 else ""
    clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"

    if strategy_id > 0:
        short_name = STRATEGY_MICRO_LABELS.get(row.get("system_key"), row.get("strategy_name", ""))
        left_label = f"🎯 {short_name} в {row.get('portfolio_name', '')}"
    else:
        left_label = f"💼 {row.get('portfolio_name', '')}"

    label_line = justify_line(left_label, f"📦 {clean_qty} шт.")
    entry_line = f"💵 Вход:\nпо {sign}{avg_price:,.2f} на {sign}{cost_basis:,.2f}"
    current_line = f"💰 Сейчас:\nпо {sign}{last_price:,.2f} на {sign}{market_val:,.2f}"
    profit_line = wrap_screen_line("📈 ", f"{p_sign}{sign}{profit:,.2f} ({p_sign}{profit_pct:.1f}%)")

    if holding_days > 0:
        annualized_pct = profit_pct * 365.0 / holding_days
        a_sign = "+" if annualized_pct >= 0 else ""
        holding_line = wrap_param_line("⏱️ ", [f"{holding_days} дн.", f"годовых {a_sign}{annualized_pct:.1f}%"])
    else:
        holding_line = wrap_screen_line("⏱️ ", f"{holding_days} дн.")

    return f"{label_line}\n{entry_line}\n{current_line}\n{profit_line}\n{holding_line}\n"


def format_broker_link_summary(alerts_count: int, orders_count: int) -> str:
    """
    Блок 5 стандарта body тикера (см. Claude/05_strategy_screen_and_kubiki.md): компактная
    сводка "Связь с брокером" -- только счётчики, полные списки -- в отдельных шторках
    (sub_view=alerts/orders), чтобы не раздувать основной экран.
    """
    return f"🔔 Алертов: {alerts_count}\n📃 Приказов: {orders_count}\n"


def format_distance_to_target(current_price: float, target_price: float, direction: str, currency_sign: str = "$") -> str:
    """
    Блок 5 стандарта body: "до цели" -- на сколько текущая цена не дошла (положительно)
    или уже прошла (отрицательно) цену приказа/алерта. direction="up" -- триггер ждёт
    РОСТА цены (продажа лимитом, тейк-профит, алерт "больше"), "down" -- ждёт ПАДЕНИЯ
    (покупка лимитом, стоп-лосс, алерт "меньше"). currency_sign -- знак валюты ЛИСТИНГА
    (не всегда "$" -- GBP/др.), берётся у вызывающего кода, не захардкожен.
    """
    distance = (target_price - current_price) if direction == "up" else (current_price - target_price)
    sign = "-" if distance < 0 else ""
    return f"до цели: {sign}{currency_sign}{abs(distance):,.2f}"


# Категория ордера -> (иконка, подпись, направление ожидаемого движения цены). Раньше
# приказы делились только на "рыночные/лимитные" vs "стоп-триггерные" -- пользователь
# уточнил (2026-07-26): правильных категорий 4, различающих купить/продать ВНУТРИ
# каждого вида (обычный лимит и защитный стоп-триггер).
ORDER_CATEGORIES = {
    "buy": ("🔸", "Покупка", "down"),
    "sell": ("🔹", "Продажа", "up"),
    "stop_loss": ("🛑", "Stop Loss", "down"),
    "take_profit": ("🎯", "Take Profit", "up"),
}

# Подписи для строки "запланировано" в Блоке 6 -- та же категоризация, что у
# ORDER_CATEGORIES, но с предлогом "К..." для покупки/продажи (читается как
# направление плана, не заголовок группы). Знак количества (см. format_cross_holdings)
# несёт тот же смысл -- продажа/SL/TP уменьшают позицию, показываются с минусом.
PLANNED_ORDER_LABELS = {
    "buy": "К покупке",
    "sell": "К продаже",
    "stop_loss": "Stop Loss",
    "take_profit": "Take Profit",
}


def classify_order(order: dict) -> str:
    """Определяет одну из 4 категорий ORDER_CATEGORIES для строки ордера."""
    order_type = int(order["type"])
    if order_type == 5:
        return "stop_loss"
    if order_type == 6:
        return "take_profit"
    return "buy" if int(order["oper"]) in (1, 2) else "sell"


def format_order_line(order: dict, current_price: float, fx_rate: float = 1.0, target_sign: str = None, show_owner: bool = False) -> str:
    """
    Блок 5 стандарта body: одна строка одного активного ордера + строка "до цели" --
    общий формат для владельческого и семейного контекста. Раньше эта логика была
    продублирована почти дословно в трёх местах tickers.py (заявки/стопы владельца,
    семейная группировка), и не показывала расстояние до цены исполнения.

    Мультивалютность (BACKLOG.md #30): order["p"]/order["stop_price"] -- нативная
    валюта листинга (брокерский слой), fx_rate переводит их в валюту смотрящего
    (current_price вызывающий код уже передаёт готовым в этой же валюте). target_sign
    по умолчанию None -> используем order["sign"] (нативный) для обратной совместимости,
    если вызывающий код ещё не прокидывает валюту смотрящего.
    """
    qty = float(order["q"])
    sign = target_sign if target_sign is not None else order["sign"]
    age = order["order_age_days"]
    category = classify_order(order)
    _, _, direction = ORDER_CATEGORIES[category]

    if category in ("stop_loss", "take_profit"):
        target_price = float(order["stop_price"] or order["p"]) * fx_rate
    else:
        target_price = float(order["p"] or 0) * fx_rate

    main_line = f"  • {qty:.0f} шт. по {sign}{target_price:,.2f} ({age} дн. назад)"
    if show_owner:
        main_line += f" — {order.get('owner_name', '')}"
    distance_line = f"      {format_distance_to_target(current_price, target_price, direction, currency_sign=sign)}"

    return f"{main_line}\n{distance_line}\n"


def format_alert_condition_line(alert: dict, current_price: float, currency_sign: str = "$", fx_rate: float = 1.0) -> str:
    """
    Блок 5 стандарта body: одна строка условия алерта -- статус + условие + (где
    применимо) расстояние до цели. Группировка по портфелям -- на стороне вызывающего
    кода (см. tickers.py), эта функция отвечает только за содержимое одной строки.
    Раньше алерт занимал развёрнутый блок в 4-5 строк (создатель/заметка/время) --
    пользователь сознательно урезал до одной строки для беглого просмотра (2026-07-26).
    currency_sign -- знак ЦЕЛЕВОЙ валюты смотрящего (BACKLOG.md #30), fx_rate --
    курс нативная валюта листинга -> валюта смотрящего, применяется к ценовым полям
    алерта (`trigger_price`/`trigger_price_min/max`, брокерский слой) -- current_price
    вызывающий код уже передаёт готовым в этой же валюте.
    """
    status_icon = "⏳" if alert["is_active"] else "🔥"
    condition_type = alert.get("condition_type")

    if alert.get("trigger_price_min") is not None and alert.get("trigger_price_max") is not None:
        p_min = float(alert['trigger_price_min']) * fx_rate
        p_max = float(alert['trigger_price_max']) * fx_rate
        condition = f"{currency_sign}{p_min:,.2f}-{currency_sign}{p_max:,.2f}"
        distance = ""
    elif alert.get("trigger_pct") is not None:
        condition = f"{condition_type}{float(alert['trigger_pct'])}%"
        distance = ""
    else:
        target_price = float(alert.get("trigger_price") or 0) * fx_rate
        condition = f"{condition_type} {currency_sign}{target_price:,.2f}"
        direction = "up" if condition_type == ">" else "down"
        distance = " " + format_distance_to_target(current_price, target_price, direction, currency_sign=currency_sign)

    return f"    {status_icon} {condition}{distance}\n"


async def format_cross_holdings(ticker_id: int, listing_id: int, portfolio_id: int = 0, strategy_id: int = 0) -> str:
    """
    Блок 6 стандарта body тикера (см. Claude/05_strategy_screen_and_kubiki.md): где ещё
    в семье есть/запланирована эта бумага -- за вычетом текущего контекста (он уже
    показан Блоком 1). Тот же переключатель, что у format_position_financials, но
    переключает не объём одной позиции, а РАЗРЕЗ списка:

    strategy_id>0 -- разрез по СТРАТЕГИЯМ: текущие холдинги через v_strategy_assets_full,
    план -- через order_pipelines (ладдер стратегии может существовать ещё без реального
    ордера у брокера). strategy_id==0 -- разрез по ПОРТФЕЛЯМ: холдинги через v_assets_full,
    план -- через РЕАЛЬНЫЕ активные ордера у брокера (public.orders), не order_pipelines --
    на уровне портфеля важен факт "выставлена ли заявка", а не абстрактный план стратегии.
    Уточнено пользователем 2026-07-26.
    """
    lines = []

    if strategy_id > 0:
        holders_sql = f"""
            SELECT strategy_name, portfolio_name, allocated_quantity AS quantity
            FROM public.v_strategy_assets_full
            WHERE listing_id = {int(listing_id)} AND allocated_quantity > 0
              AND strategy_id != {int(strategy_id)};
        """
        holders = await asyncio.to_thread(db_bot.execute_query, holders_sql)
        holders = holders if isinstance(holders, list) else ([holders] if holders else [])

        planned_sql = f"""
            SELECT s.strategy_name, p.name AS portfolio_name, op.target_quantity
            FROM public.order_pipelines op
            JOIN public.strategies s ON s.id = op.strategy_id
            JOIN public.portfolios p ON p.id = op.portfolio_id
            WHERE op.ticker_id = {int(ticker_id)} AND op.pipeline_status IN ('PENDING', 'ACTIVE')
              AND op.strategy_id != {int(strategy_id)};
        """
        planned = await asyncio.to_thread(db_bot.execute_query, planned_sql)
        planned = planned if isinstance(planned, list) else ([planned] if planned else [])

        lines.append("📍 **В стратегиях семьи:**")
        for h in holders:
            qty = float(h["quantity"])
            clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"
            lines.append(f" • {h['strategy_name']} ({h['portfolio_name']}) — {clean_qty} шт.")
        for pl in planned:
            # Знак target_quantity уже задан по конвенции проекта (см.
            # analytics_utils.py::expected_step_quantity): >0 вход (покупка), <0 выход (продажа)
            qty = float(pl["target_quantity"])
            direction = "К покупке" if qty >= 0 else "К продаже"
            clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"
            lines.append(f" 📋 {direction} у {pl['strategy_name']} ({pl['portfolio_name']}): {clean_qty} шт.")
        if not holders and not planned:
            lines.append("   *Больше нигде нет.*")

    else:
        holders_sql = f"SELECT owner_name, portfolio_name, quantity FROM public.v_assets_full WHERE listing_id = {int(listing_id)} AND quantity > 0"
        if portfolio_id:
            holders_sql += f" AND portfolio_id != {int(portfolio_id)}"
        holders = await asyncio.to_thread(db_bot.execute_query, holders_sql + ";")
        holders = holders if isinstance(holders, list) else ([holders] if holders else [])

        planned_sql = f"""
            SELECT u.name AS owner_name, p.name AS portfolio_name, o.q, o.type, o.oper
            FROM public.orders o
            JOIN public.listings l ON o.listing_id = l.id
            JOIN public.portfolios p ON o.portfolio_id = p.id
            JOIN public.users u ON p.owner_id = u.id
            WHERE l.ticker_id = {int(ticker_id)} AND o.status IN ('active', 'NEW', 'PARTIALLY_FILLED')
        """
        if portfolio_id:
            planned_sql += f" AND o.portfolio_id != {int(portfolio_id)}"
        planned = await asyncio.to_thread(db_bot.execute_query, planned_sql + ";")
        planned = planned if isinstance(planned, list) else ([planned] if planned else [])

        lines.append("📍 **В портфелях семьи:**")
        for h in holders:
            qty = float(h["quantity"])
            clean_qty = int(qty) if qty.is_integer() else f"{qty:.2f}"
            lines.append(f" • {h['owner_name']} ({h['portfolio_name']}) — {clean_qty} шт.")
        for pl in planned:
            # Та же категоризация, что у реальных ордеров в Блоке 5 (покупка/продажа/
            # SL/TP) -- знак количества показывает направление (продажа/SL/TP -- минус)
            category = classify_order(pl)
            label = PLANNED_ORDER_LABELS[category]
            qty = float(pl["q"])
            signed_qty = -qty if category != "buy" else qty
            clean_qty = int(signed_qty) if float(signed_qty).is_integer() else f"{signed_qty:.2f}"
            lines.append(f" 📋 {label} у {pl['owner_name']} ({pl['portfolio_name']}): {clean_qty} шт.")
        if not holders and not planned:
            lines.append("   *Больше нигде нет.*")

    return "\n".join(lines) + "\n"


# Отображение долгосрочного ценового тренда (signal_recommendation) -- намеренно не
# BUY/SELL, чтобы не путать с реальными сигналами конкретных стратегий (см.
# analytics/analytics_utils.py и BACKLOG.md). Fallback покрывает и старые значения
# STRONG_BUY/SELL/NEUTRAL, ещё не перезаписанные ночной синхронизацией. Раньше жила
# только в ticker_search.py -- теперь общая, используется и основной карточкой тикера.
TREND_LABELS = {
    "UPTREND": ("📈", "РАСТЁТ"),
    "DOWNTREND": ("📉", "ПАДАЕТ"),
    "NEUTRAL": ("➡️", "БОКОВИК"),
    "STRONG_BUY": ("📈", "РАСТЁТ"),
    "SELL": ("📉", "ПАДАЕТ"),
}


async def format_market_signals(ticker_id: int) -> str:
    """
    Блок 4 стандарта body тикера (см. Claude/05_strategy_screen_and_kubiki.md): рыночные
    сигналы -- компакт-версия (тренд, RSI, отклонение от SMA200). Раньше существовал
    только в ticker_search.py (быстрый поиск) -- отсутствовал на основной карточке
    тикера, пробел зафиксирован пользователем 2026-07-26. Компакт -- те же 3 вопроса,
    которые проверяют в первую очередь (куда идёт тренд, не перекуплена/перепродана ли
    бумага, выше/ниже своей долгосрочной средней) -- те же поля, на которых буквально
    строятся критерии стратегий (см. Блок 9, "обоснование идеи"). Без "подробнее" --
    короче фундаментала, разворачивать пока незачем (решение по умолчанию, можно
    добавить позже, если понадобится).
    """
    t = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT signal_recommendation, signal_rsi, signal_price_to_sma200_pct
            FROM public.tickers WHERE id = {int(ticker_id)};
        """
    )
    if not t:
        return ""

    raw_trend = str(t.get('signal_recommendation') or 'NEUTRAL').upper()
    trend_icon, trend_label = TREND_LABELS.get(raw_trend, ("🔸", raw_trend.replace('_', ' ')))
    rsi = float(t.get('signal_rsi') or 0)
    sma200_pct = float(t.get('signal_price_to_sma200_pct') or 0)

    return (
        f"📢 Тренд: **{trend_icon} {trend_label}**\n"
        f"📊 RSI: **{rsi:.1f}** • SMA200: **{sma200_pct:+.1f}%**\n"
    )


async def format_ticker_behavior(ticker_id: int, listing_id: int = 0, portfolio_id: int = 0) -> str:
    """
    Блок 2 стандарта body тикера (см. Claude/05_strategy_screen_and_kubiki.md,
    Claude/07_glossary.md): "Поведение" -- простое % изменение цены за 1д/1нед/1мес/1год
    (`tickers.signal_pct_1d/1w/1m/1y`, считаются в site_connectors/sync_signals_yf.py из
    того же годового ряда Yahoo, что и SMA/RSI в Блоке 4 -- но это ДРУГОЕ число: не
    отклонение от сглаженной средней, а буквальное "на сколько изменилась цена", проще
    читать новичку).

    Индикатор резкого движения -- больше не заглушка: PriceMoveWatcher
    (analytics/price_move_watcher.py) взводит алерт (source_type='uport') в той же
    таблице public.alerts, что и брокерские; здесь просто читаем, есть ли активный.
    portfolio_id>0 -- фильтруем на алерт ИМЕННО ЭТОГО портфеля (нашли живой баг: без
    фильтра карточка П10 показывала алерт П136, просто как "последний по времени" --
    у разных портфелей, держащих одну бумагу, алерты отдельные записи, см.
    price_move_watcher.py). portfolio_id==0 (семейный свод) -- без фильтра, любой.
    """
    t = await asyncio.to_thread(
        db_bot.execute_row,
        f"""
            SELECT signal_pct_1d, signal_pct_1w, signal_pct_1m, signal_pct_1y
            FROM public.tickers WHERE id = {int(ticker_id)};
        """
    )
    if not t:
        return ""

    pct_1d = float(t.get('signal_pct_1d') or 0)
    pct_1w = float(t.get('signal_pct_1w') or 0)
    pct_1m = float(t.get('signal_pct_1m') or 0)
    pct_1y = float(t.get('signal_pct_1y') or 0)

    move_alert = None
    if listing_id:
        portfolio_filter = f"AND portfolio_id = {int(portfolio_id)}" if portfolio_id else ""
        move_alert = await asyncio.to_thread(
            db_bot.execute_row,
            f"""
                SELECT note FROM public.alerts
                WHERE listing_id = {int(listing_id)} AND source_type = 'uport' AND is_active = true
                {portfolio_filter}
                ORDER BY triggered_at DESC LIMIT 1;
            """
        )
    move_line = f"📢 {move_alert['note']}\n" if move_alert else "✅ Резких движений нет\n"

    # Моноширинный code-спан только для этой колонки чисел -- обычный пропорциональный
    # шрифт сообщений даёт "кривой" правый край на нескольких строках подряд (посимвольное
    # выравнивание тут недостаточно, в отличие от одной строки шапки), а backtick-код
    # рендерится Telegram моноширинно -- то же посимвольное выравнивание даёт ровный край.
    return (
        f"📉 Поведение:\n"
        f"`{justify_line('День', f'{pct_1d:+.1f}%')}`\n"
        f"`{justify_line('Неделя', f'{pct_1w:+.1f}%')}`\n"
        f"`{justify_line('Месяц', f'{pct_1m:+.1f}%')}`\n"
        f"`{justify_line('Год', f'{pct_1y:+.1f}%')}`\n"
        f"{move_line}"
    )


async def format_strategy_header(strategy_id: int) -> str:
    """
    Универсальный сборщик шапки карточки стратегии UPort (см. Claude/BACKLOG.md #13).
    Список стратегий портфеля использует свою отдельную LEGO-строку
    (`generate_strategy_button_text`, однострочный формат для кнопки, не подходит
    для многострочной шапки) -- эта функция используется только самой карточкой.
    """
    sql = f"""
        SELECT s.strategy_name, s.strategy_share_pct, s.human_philosophy, s.is_active,
               p.name AS portfolio_name, p.id AS portfolio_id,
               u.name AS owner_name
        FROM public.strategies s
        JOIN public.portfolios p ON s.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        WHERE s.id = {int(strategy_id)}
        LIMIT 1;
    """
    s = await asyncio.to_thread(db_bot.execute_row, sql)

    if not s:
        return "🎯 **Стратегия UPort**\n❌ Стратегия не найдена в СУБД."

    share_pct = float(s.get("strategy_share_pct") or 0.0)
    philosophy = s.get("human_philosophy") or "Описание философии стратегии не задано."
    status_badge = "" if s.get("is_active") else " ⏸️ (неактивна)"

    header = (
        f"🎯 **{s.get('strategy_name', 'Без названия')}**{status_badge}\n"
        f"💼 Портфель {s.get('portfolio_name', '')} ({s.get('owner_name', 'Unknown')})\n"
        f"📐 Целевая доля капитала: **{share_pct:.0f}%**\n"
        f"{SEPARATOR_LINE}\n"
        f"_{philosophy}_\n"
        f"{SEPARATOR_LINE}\n"
    )
    return header


async def format_strategy_capital_block(portfolio_id: int, strategy_id: int, target_sign: str = "$", fx_rate: float = 1.0) -> str:
    """
    Блок body карточки стратегии: план/факт капитала (идеальный бюджет / текущая стоимость
    позиций / свободный остаток) -- из PortfolioInspector.get_virtual_cash_balances().

    Мультивалютность: PortfolioInspector считает всегда в USD (внутренний хаб-расчёт,
    см. Claude/07_glossary.md), fx_rate -- курс USD -> валюта СМОТРЯЩЕГО пользователя,
    посчитанный один раз за рендер вызывающим кодом (тот же принцип, что у
    format_position_financials для тикера).
    """
    inspector = await asyncio.to_thread(PortfolioInspector, db_bot, portfolio_id)
    balances = await asyncio.to_thread(inspector.get_virtual_cash_balances)
    strat_balance = balances.get("strategies", {}).get(strategy_id)

    if not strat_balance:
        return ""

    ideal = float(strat_balance["ideal_budget_usd"]) * fx_rate
    current = float(strat_balance["current_holdings_usd"]) * fx_rate
    free = float(strat_balance["virtual_free_cash_usd"]) * fx_rate

    return (
        f"💰 **План/факт капитала:**\n"
        f" • Идеальный бюджет: **{target_sign}{ideal:,.2f}**\n"
        f" • Текущая стоимость позиций: **{target_sign}{current:,.2f}**\n"
        f" • Свободный остаток: **{target_sign}{free:,.2f}**\n"
        f"{SEPARATOR_LINE}\n"
    )


async def format_strategy_risk_audit_block(portfolio_id: int, strategy_id: int) -> str:
    """
    Блок body карточки стратегии: аудит лимитов и налоговых рисков стратегии -- из
    PortfolioInspector.audit_limits_and_rules(). Только проценты, валюты не касается.
    """
    inspector = await asyncio.to_thread(PortfolioInspector, db_bot, portfolio_id)
    audit = await asyncio.to_thread(inspector.audit_limits_and_rules)
    strat_audit = audit.get("strategies", {}).get(strategy_id)

    if strat_audit and strat_audit.get("violation_found"):
        text = "🛡️ **Нарушения лимитов стратегии:**\n"
        for a in strat_audit.get("violated_assets", []):
            text += f" ⚠️ {a['symbol']}: доля {a['current_share_pct']:.1f}% (лимит {a['limit_pct']}%)\n"
        for sec in strat_audit.get("violated_sectors", []):
            text += f" ⚠️ Сектор {sec.get('sector', '?')}: доля {sec.get('current_share_pct', 0):.1f}% (лимит {sec.get('limit_pct', 0)}%)\n"
        for t in strat_audit.get("tax_shield_breaches", []):
            text += f" ⚠️ {t['symbol']}: дивиденды {t['dividend_yield_pct']:.1f}% (лимит {t['limit_pct']}%)\n"
        for u in strat_audit.get("unassigned_assets", []):
            text += f" ⚠️ {u['symbol']}: не распределена по стратегии ({u['current_share_pct']:.1f}% капитала)\n"
        text += f"{SEPARATOR_LINE}\n"
    else:
        text = f"🛡️ Лимиты и налоговые риски стратегии соблюдены.\n{SEPARATOR_LINE}\n"

    return text


async def format_portfolio_risk_audit_rollup(portfolio_id: int) -> str:
    """
    Блок body карточки портфеля (вкладка passport): роллап риск-аудита по ВСЕМ активным
    стратегиям портфеля -- заменяет старый блок "Соответствие стратегии", завязанный на
    легаси-поля portfolios.strategy_type/risk_profile/etc (см. Claude/BACKLOG.md #17).
    Один PortfolioInspector, один вызов audit_limits_and_rules() -- не по одному на
    стратегию, чтобы не плодить лишние пересчёты total_capital.
    """
    inspector = await asyncio.to_thread(PortfolioInspector, db_bot, portfolio_id)
    audit = await asyncio.to_thread(inspector.audit_limits_and_rules)

    if not audit.get("has_violations"):
        return "🛡️ Нарушений лимитов и налоговых рисков не найдено ни в одной стратегии.\n"

    text = "🛡️ **Нарушения лимитов по стратегиям:**\n"
    for strat_audit in audit.get("strategies", {}).values():
        if not strat_audit.get("violation_found"):
            continue
        text += f"🎯 {strat_audit.get('strategy_name', '')}:\n"
        for a in strat_audit.get("violated_assets", []):
            text += f" ⚠️ {a['symbol']}: доля {a['current_share_pct']:.1f}% (лимит {a['limit_pct']}%)\n"
        for sec in strat_audit.get("violated_sectors", []):
            text += f" ⚠️ Сектор {sec.get('sector', '?')}: доля {sec.get('current_share_pct', 0):.1f}% (лимит {sec.get('limit_pct', 0)}%)\n"
        for t in strat_audit.get("tax_shield_breaches", []):
            text += f" ⚠️ {t['symbol']}: дивиденды {t['dividend_yield_pct']:.1f}% (лимит {t['limit_pct']}%)\n"
        for u in strat_audit.get("unassigned_assets", []):
            text += f" ⚠️ {u['symbol']}: не распределена по стратегии ({u['current_share_pct']:.1f}% капитала)\n"

    return text


def generate_confirm_screen(header_text: str, action_title: str, details_list: list, parse_mode: str = "Markdown") -> str:
    """
    Универсальная двухрежимная подпрограмма генерации экранов подтверждения (Шторок).
    Поддерживает стандарты Markdown и HTML экосистемы UPort.
    """
    # Задаем разметку в зависимости от выбранного режима хэндлера
    if parse_mode.upper() == "HTML":
        b_open, b_close = "<b>", "</b>"
        i_open, i_close = "<i>", "</i>"
    else:
        b_open, b_close = "**", "**"
        i_open, i_close = "*", "*"

    # Собираем скелет экрана подтверждения
    lines = [
        f"{header_text}",
        f"❓ {b_open}{action_title.upper()}{b_close}"
    ]

    # Наполняем контент специфическими строками операции
    for item in details_list:
        # Если строка содержит разделители, оформляем её с красивым сдвигом UPort
        if "：" in item or ":" in item:
            # Бьем строку по двоеточию, чтобы сделать внутренний акцент жирным или курсивом
            separator = "：" if "：" in item else ":"
            parts = item.split(separator, 1)
            title = parts[0].strip()
            value = parts[1].strip()
            lines.append(f"└ {i_open}{title}{i_close}{separator} {b_open}{value}{b_close}")
        else:
            lines.append(f"└ {item}")

    return "\n".join(lines)
