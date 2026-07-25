import logging
import asyncio
import yfinance as yf
from database import db_bot

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


async def format_premium_header(ticker_id: int, portfolio_id: int = 0) -> str:
    """
    Универсальный сборщик премиальной шапки тикера UPort (общий header/body/footer
    стандарт -- см. Claude/05_strategy_screen_and_kubiki.md, BACKLOG.md #19).

    Цена берётся из public.listings, если бумага уже легализована у какого-то брокера
    (предпочтительно -- у брокера portfolio_id, если он есть; иначе любой существующий
    листинг); если листинга ещё нет вообще (бумага только что найдена глобальным
    поиском, ещё не добавлена в список наблюдения) -- цена берётся напрямую с Yahoo
    Finance. Источник цены (короткое имя брокера или "YF") подписан явно, чтобы не
    путать слои (см. BACKLOG.md #21 -- смешение канонической цены tickers и брокерской
    цены listings уже давало реальный баг).
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
            # Цена из брокерского листинга -- своя валюта/единицы, применяем multiplier слоя listings
            raw_price = float(l_row.get("last_price") or 0.0)
            curr_id = l_row.get("currency_id", "USD")
            c_row = await asyncio.to_thread(
                db_bot.execute_row, f"SELECT sign, multiplier FROM public.currencies WHERE id = '{curr_id}' LIMIT 1;"
            )
            sign = c_row.get("sign", f"{curr_id} ")
            multiplier = float(c_row.get("multiplier") or 1.0)
            live_price = raw_price * multiplier
            price_source = l_row.get("broker_short_name") or "брокер"
        else:
            # Листинга ещё нет нигде -- берём цену напрямую с Yahoo Finance, канонический
            # тикер/валюта -- из public.tickers (без multiplier листингов, слои не смешиваем)
            ticker_name_map = t.get("ticker_name_map") or {}
            yahoo_symbol = ticker_name_map.get("YAHOO", display_symbol)
            try:
                fast_info = await asyncio.to_thread(lambda: yf.Ticker(yahoo_symbol).fast_info)
                live_price = float(fast_info.get("last_price") or fast_info.get("open") or 0.0)
            except Exception as yf_err:
                logging.warning(f"⚠️ [BOT SCREENS]: Не удалось получить живую цену Yahoo для {yahoo_symbol}: {yf_err}")
                live_price = 0.0
            curr_id = t.get("currency_id") or "USD"
            sign = "$" if curr_id == "USD" else ("£" if curr_id == "GBP" else f"{curr_id} ")
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
            f"💵 {price_source}: **{sign}{live_price:,.2f}**"
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

async def format_strategy_header(strategy_id: int) -> str:
    """
    Универсальный сборщик шапки карточки стратегии UPort.
    Переиспользуется списком стратегий портфеля и самой карточкой стратегии,
    чтобы не дублировать формат в двух местах (см. Claude/BACKLOG.md #13).
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
