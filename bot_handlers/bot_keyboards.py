import asyncio
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup
from database import db_bot
from bot_handlers.common import MenuAction

# ⚙️ СИСТЕМНЫЕ КОНСТАНТЫ И МИКРО-СТАНКИ ВЕРСТКИ LEGO-СТРОК UPORT
EN_SPACE = "\u2002"       # Фиксированный пробел
FIGURE_SPACE = "\u2007"   # Цифровой пробел (равен ширине одной цифры)
BRAILLE_EMPTY = "\u2800"  # Прозрачный знак Брайля
SIX_PER_EM = "\u2006"     # Тончайший микро-отступ (1/6 пробела)


def build_smart_badge(icon: str, value: int) -> str:
    """
    Пункт 1: Преобразователь одной иконки и индекса в кубик фиксированной ширины.
    - value > 9: Перегрузка индекса превращается в знак '⁺'.
    - value == 0 или нет иконки: Возвращает калиброванную невидимую заглушку 
      из пустого символа Брайля и шестого пробела для удержания ширины.
    """
    # Состояние 3: Пустой кубик (Иконки нет). Ставим прозрачную заглушку UPort
    if value == 0 or not icon:
        BRAILLE_EMPTY = "\u2800"  # Полноразмерный пустой знак Брайля
        SIX_PER_EM = "\u2006"     # Микро-отступ (1/6 ширины пробела) для точной калибровки
        return f"{BRAILLE_EMPTY}{SIX_PER_EM}"
        
    # Состояние 2: Перегрузка индекса (Число больше 9)
    if value > 9:
        superscript = "⁺"
    # Состояние 1: Стандартный полный кубик (Индекс от 1 до 9)
    else:
        superscripts = {
            '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
            '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
        }
        superscript = superscripts.get(str(value), '⁰')
        
    # Возвращаем живой графический кубик-бэдж
    return f"{icon}{superscript}"

def generate_confirm_keyboard(yes_text: str, yes_callback_packed: str, no_text: str, no_callback_packed: str) -> InlineKeyboardMarkup:
    """
    Универсальный пульт подтверждения критических операций (Да / Нет).
    Принимает готовые тексты кнопок и уже упакованные строки callback_data (.pack()).
    Если no_text пустой, генерирует лаконичную одиночную кнопку (для экранов успеха).
    """
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text=yes_text, callback_data=yes_callback_packed))
    if no_text:
        builder.row(types.InlineKeyboardButton(text=no_text, callback_data=no_callback_packed))
    return builder.as_markup()

async def generate_target_strategies_keyboard(portfolio_id: int, ticker_id: int, source_strategy_id: int, quantity: float) -> InlineKeyboardMarkup:
    """
    Динамически собирает пульт выбора альтернативных стратегий-приемников (куда перенести доли).
    Исключает исходную стратегию на уровне СУБД. В callback_data уходят СТРОГО числовые ID.
    """
    builder = InlineKeyboardBuilder()
    
    sql = f"""
        SELECT id, strategy_name 
        FROM public.strategies 
        WHERE portfolio_id = {int(portfolio_id)} 
          AND is_active = true 
          AND id != {int(source_strategy_id)};
    """
    strategies = await asyncio.to_thread(db_bot.execute_query, sql)
    
    if isinstance(strategies, list):
        for strat in strategies:
            strat_id = int(strat['id'])
            
            # Упаковываем СТРОГО числовые ID. Текстовые имена в callback_data запрещены.
            builder.row(types.InlineKeyboardButton(
                text=f"📥 {strat['strategy_name']}",
                callback_data=MenuAction(
                    action="move_confirm",
                    portfolio_id=int(portfolio_id),
                    ticker_id=int(ticker_id),
                    sub_view=f"{int(source_strategy_id)}/{int(quantity)}",
                    task_id=strat_id
                ).pack()
            ))
            
    builder.row(types.InlineKeyboardButton(text="💤 Оставить в покое", callback_data=MenuAction(action="main_menu").pack()))
    return builder.as_markup()

def generate_nav_back_keyboard(one_step_back_text: str, full_back_callback: str) -> InlineKeyboardMarkup:
    """
    Абсолютно универсальный навигационный пульт возврата экосистемы UPort.
    Генерирует пару кнопок: "Один шаг назад" (с полной сохранностью контекста через full_back_callback)
    и жесткую системную кнопку "В главное меню".
    """
    builder = InlineKeyboardBuilder()
    
    # Кнопка динамического возврата на один шаг назад
    builder.row(types.InlineKeyboardButton(
        text=one_step_back_text,
        callback_data=full_back_callback
    ))
    
    # Кнопка безусловного возврата в Главное меню UPort
    builder.row(types.InlineKeyboardButton(
        text="📱 В главное меню",
        callback_data=MenuAction(action="main_menu").pack()
    ))
    
    return builder.as_markup()

def generate_main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Генерирует интерактивный пульт Главного меню экосистемы UPort.
    Проверяет гибкий флаг админа для отображения скрытых инженерных настроек.
    """
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="🔬 Списки наблюдения", callback_data=MenuAction(action="show_watchlist_focus").pack()))
    builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены рынка", callback_data=MenuAction(action="update_prices").pack()))
    builder.row(types.InlineKeyboardButton(text="🛠️ Бэклог разработки", callback_data=MenuAction(action="backlog_main").pack()))
    
    if is_admin:
        builder.row(types.InlineKeyboardButton(text="⚙️ Настройки системы", callback_data=MenuAction(action="settings_main").pack()))
        
    return builder.as_markup()

def build_smart_badge(icon: str, value: int) -> str:
    """
    Микро-станок 1: Преобразователь одной иконки и индекса в кубик фиксированной ширины.
    - value > 9: Перегрузка индекса превращается в знак '⁺'.
    - value == 0 или нет иконки: Возвращает калиброванную невидимую заглушку UPort.
    """
    if not icon:
        return f"{BRAILLE_EMPTY}{BRAILLE_EMPTY}{SIX_PER_EM}{SIX_PER_EM}{SIX_PER_EM}"
        
    if value == 0:
        superscript = SIX_PER_EM
    elif value > 9:
        superscript = "⁺"
    else:
        superscripts = {
            '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', 
            '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
        }
        superscript = superscripts.get(str(value), '⁰')
        
    return f"{icon}{superscript}"

def build_ticker_block(ticker: str) -> str:
    """
    Микро-станок 2: Превращает любой тикер в жесткий блок фиксированной ширины.
    Выносит тикер влево, добивает en_space до 5 знаков + Брайль-заглушка.
    """
    clean_ticker = str(ticker).strip().upper()
    padding_count = max(0, 5 - len(clean_ticker))
    return f"{clean_ticker}{EN_SPACE * padding_count}{BRAILLE_EMPTY}{BRAILLE_EMPTY}"

def build_number_block(value: int, suffix: str = "шт", show_sign: bool = False) -> str:
    """
    Микро-станок 3: Бухгалтерское форматирование целых чисел (объемов штук).
    Знак числа приклеивается вплотную к первой живой цифре.
    """
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    abs_value = abs(value)
    raw_qty_str = f"{int(abs_value):04d}"
    
    first_live_idx = 3
    for idx, char in enumerate(raw_qty_str):
        if char != '0':
            first_live_idx = idx
            break

    final_block = ""
    needed_paddings = first_live_idx
    
    if sign_char == "":
        final_block += FIGURE_SPACE + (FIGURE_SPACE * needed_paddings)
    else:
        final_block += (FIGURE_SPACE * needed_paddings) + sign_char

    final_block += raw_qty_str[first_live_idx:]
    return f"{final_block}{suffix}"

def build_finance_block(value: float, currency_symbol: str = "$", show_sign: bool = False) -> str:
    """
    Микро-станок 4: Сверхточный финансовый блок результатов (Валюта + Цена/Прибыль).
    Знак и символ валюты приклеиваются вплотную к первой живой цифре числа.
    """
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    abs_value = abs(value)
    raw_finance_str = f"{abs_value:07.2f}"
    
    parts = raw_finance_str.split(".")
    int_part = parts[0]
    dec_part = parts[1]

    first_live_idx = 3
    for idx, char in enumerate(int_part):
        if char != '0':
            first_live_idx = idx
            break

    final_block = ""
    needed_paddings = first_live_idx

    if sign_char == "":
        final_block += FIGURE_SPACE + (FIGURE_SPACE * needed_paddings) + currency_symbol
    else:
        final_block += (FIGURE_SPACE * needed_paddings) + sign_char + currency_symbol

    final_block += int_part[first_live_idx:] + "." + dec_part
    return final_block

def build_percent_block(value: float, show_sign: bool = False) -> str:
    """
    Микро-станок 5: Финансовое форматирование процентов (доходности).
    Знак приклеивается вплотную к первой живой цифре числа.
    """
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    abs_value = abs(value)
    raw_pct_str = f"{abs_value:05.2f}"
    
    parts = raw_pct_str.split(".")
    int_part = parts[0]
    dec_part = parts[1]

    first_live_idx = 1
    for idx, char in enumerate(int_part):
        if char != '0':
            first_live_idx = idx
            break

    final_block = ""
    needed_paddings = first_live_idx

    if sign_char == "":
        final_block += FIGURE_SPACE + (FIGURE_SPACE * needed_paddings)
    else:
        final_block += (FIGURE_SPACE * needed_paddings) + sign_char

    final_block += int_part[first_live_idx:] + "." + dec_part + "%"
    return final_block

def build_separator_block(char: str) -> str:
    """
    Микро-станок 6: Генерирует фиксированный по ширине разделитель кубиков.
    """
    clean_char = str(char).strip()
    if clean_char == "" or clean_char == " ":
        return f"{BRAILLE_EMPTY}{BRAILLE_EMPTY}"
    return f"{BRAILLE_EMPTY}{clean_char}{BRAILLE_EMPTY}"

def assemble_lego_line(json_blueprint: list) -> str:
    """
    Универсальный сборочный конвейер LEGO-строк UPort.
    """
    final_string = ""
    for block in json_blueprint:
        b_type = block.get("type")
        
        if b_type == "badge":
            final_string += build_smart_badge(icon=block.get("icon", ""), value=int(block.get("index", 0)))
        elif b_type == "ticker":
            final_string += build_ticker_block(ticker=block.get("value", ""))
        elif b_type == "number":
            final_string += build_number_block(value=int(block.get("value", 0)), suffix=block.get("suffix", "шт"), show_sign=bool(block.get("show_sign", False)))
        elif b_type == "finance":
            final_string += build_finance_block(value=float(block.get("value", 0.0)), currency_symbol=block.get("currency_symbol", "$"), show_sign=bool(block.get("show_sign", False)))
        elif b_type == "percent":
            final_string += build_percent_block(value=float(block.get("value", 0.0)), show_sign=bool(block.get("show_sign", False)))
        elif b_type == "separator":
            final_string += build_separator_block(char=block.get("value", ""))
        elif b_type == "final_row":
            block["value"] = final_string
            
    return final_string

# 🏗️ ВЫСОКОУРОВНЕВЫЕ ВЫЗЫВАЕМЫЕ ГЕНЕРАТОРЫ СТРОК ДЛЯ ИНЛАЙН-КНОПОК

def generate_portfolio_button_text(crystal: str, ticker: str, quantity: int, profit: float, profit_pct: float) -> str:
    """
    Генерирует жесткую монолитную строку для инлайн-кнопок Состава Портфеля.
    """
    blueprint = [
        {"type": "badge", "icon": crystal, "index": 0},
        {"type": "ticker", "value": ticker},
        {"type": "number", "value": quantity, "suffix": "шт", "show_sign": False},
        {"type": "finance", "value": profit, "currency_symbol": "$", "show_sign": False},
        {"type": "percent", "value": profit_pct, "show_sign": True},
        {"type": "final_row", "value": ""}
    ]
    return assemble_lego_line(blueprint)

def generate_watchlist_button_text(ticker: str, f1: str, f2: str, f3: str, f4: str, f5: str, alert_icon: str, alerts_count: int) -> str:
    """
    Генерирует жесткую монолитную строку для инлайн-кнопок Списков Наблюдения.
    🔥 ФИНАЛ: Полностью многомерный радар. Каждое знакоместо управляется независимо.
    """
    blueprint = [
        {"type": "ticker", "value": ticker},
        {"type": "separator", "value": " "},
        {"type": "badge", "icon": f1, "index": 0},          # Фаза 1: Изучение (🔍)
        {"type": "badge", "icon": f2, "index": 0},          # Фаза 2: Наблюдение (🎯)
        {"type": "badge", "icon": f3, "index": 0},          # Фаза 3: Ордер (📃)
        {"type": "badge", "icon": f4, "index": 0},          # Фаза 4: Портфель (💼)
        {"type": "badge", "icon": f5, "index": 0},          # Фаза 5: Распродано (🏁)
        {"type": "badge", "icon": alert_icon, "index": alerts_count},  # Колокольчик алертов (Управляется из хэндлера)
        {"type": "final_row", "value": ""}
    ]
    return assemble_lego_line(blueprint)
