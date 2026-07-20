import logging
import asyncio
from database import db_bot

async def format_premium_header(ticker_id: int, portfolio_id: int) -> str:
    """
    Универсальный сборщик укороченной премиальной шапки UPort.
    Берет точную котировку last_price и валюту строго из таблицы listings брокера.
    🔥 МОДЕРНИЗАЦИЯ: Использует безопасный метод execute_row экосистемы UPort.
    """
    try:
        # 1. Запрашиваем паспорт инструмента
        sql_t = f"""
            SELECT t.symbol, t.company_name, t.asset_type, t.sector, t.industry, e.exchange_code
            FROM public.tickers t
            LEFT JOIN public.exchanges e ON t.exchange_mic = e.mic
            WHERE t.id = {int(ticker_id)} LIMIT 1;
        """
        t = await asyncio.to_thread(db_bot.execute_row, sql_t)
        
        if not t:
            return "🔬 **Инструмент UPort**\n❌ Паспорт бумаги не найден в СУБД."

        # 2. Запрашиваем данные листинга
        sql_l = f"SELECT last_price, currency_id FROM public.listings WHERE ticker_id = {int(ticker_id)} LIMIT 1;"
        l_row = await asyncio.to_thread(db_bot.execute_row, sql_l)
        
        raw_price = float(l_row.get("last_price") or 0.0)
        curr_id = l_row.get("currency_id", "USD")

        # 3. Запрашиваем параметры валюты листинга
        sql_c = f"SELECT sign, multiplier FROM public.currencies WHERE id = '{curr_id}' LIMIT 1;"
        c_row = await asyncio.to_thread(db_bot.execute_row, sql_c)
        
        sign = c_row.get("sign", f"{curr_id} ")
        multiplier = float(c_row.get("multiplier") or 1.0)
        live_price = raw_price * multiplier

        # 4. Формируем текстовые маркеры дизайна
        display_symbol = t.get("symbol", "N/A")
        asset_type = str(t.get('asset_type', 'EQUITY')).upper().strip()
        type_badge = "БИРЖЕВОЙ ФОНД (ETF)" if asset_type == 'ETF' else "АКЦИЯ"
        exch_code = t.get("exchange_code") or "GLOBAL"

        header = (
            f"🔬 **{display_symbol}**\n"
            f"🏢 {t.get('company_name', 'Unknown')[:35]}\n"
            f"📦 {t.get('sector', 'N/A')} — {t.get('industry', 'N/A')}\n"
            f"🏷️ {type_badge}, {exch_code}\n"
            f"───────\n"
            f"💵 Цена рынка: **{sign}{live_price:,.2f}**\n"
        )
        return header
    except Exception as e:
        logging.error(f"🚨 [BOT SCREENS ERROR]: Сбой сборщика шапки id={ticker_id}: {e}")
        return "🔬 **Инструмент UPort**\n⚠️ Ошибка сбора паспортных данных."

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

    lines.append(f"\nБудет произведен атомарный сдвиг долей в СУБД.")
    return "\n".join(lines)
