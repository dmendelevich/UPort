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

def generate_tab_switch_keyboard(tabs: list, current_sub_view: str) -> InlineKeyboardMarkup:
    """
    Универсальный подвал-переключатель вкладок одного экрана (см. Claude/BACKLOG.md #13).
    tabs -- список пар (текст кнопки, MenuAction). Рендерит кнопки всех вкладок,
    КРОМЕ той, чей sub_view совпадает с current_sub_view (уже открытую вкладку не показываем).
    """
    builder = InlineKeyboardBuilder()
    buttons = [
        types.InlineKeyboardButton(text=label, callback_data=action.pack())
        for label, action in tabs
        if action.sub_view != current_sub_view
    ]
    if buttons:
        builder.row(*buttons)
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

def generate_ticker_footer_keyboard(
    portfolio_id: int, listing_id: int, symbol: str, strategy_id: int, is_owner_view: bool,
    alerts_count: int, orders_count: int, back_text: str, back_callback: str,
    watchlist_removable: bool = False
) -> InlineKeyboardMarkup:
    """
    Блок 5 стандарта body (см. Claude/05_strategy_screen_and_kubiki.md) -- компактные
    Алерты/Приказы + условная кнопка «План» (только владельцу, ведёт на общую вкладку
    "plan" из tickers.py, где уже разветвляется на "Привязать ордер"/"План входа"/
    "План выхода"/просмотр) + навигация назад. Общий кубик для любой карточки тикера
    с портфельным контекстом (полная карточка в tickers.py, обоснование идеи в
    «Предложениях» и т.п.) -- выделен 2026-07-29, см. Claude/BACKLOG.md.

    watchlist_removable -- бумага сейчас в СН этого портфеля И безопасна к удалению
    (см. bot_handlers/watchlist.py::get_watchlist_removal_status) -- вызывающий код уже
    сходил в БД и решил, эта функция сама в БД не лезет (тот же принцип, что и для
    alerts_count/orders_count выше). По умолчанию False -- сегодня вычисляется только
    в tickers.py (карточка из СН), не в «Предложениях» (strategies.py) — можно
    подключить туда позже тем же параметром, если понадобится.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text=f"🔔 Алерты ({alerts_count})",
            callback_data=MenuAction(
                action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id,
                ticker_name=symbol, sub_view="alerts/from_ticker", strategy_id=strategy_id
            ).pack()
        ),
        types.InlineKeyboardButton(
            text=f"📃 Приказы ({orders_count})",
            callback_data=MenuAction(
                action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id,
                ticker_name=symbol, sub_view="orders", strategy_id=strategy_id
            ).pack()
        )
    )

    if is_owner_view:
        builder.row(types.InlineKeyboardButton(
            text="📋 План",
            callback_data=MenuAction(
                action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id,
                ticker_name=symbol, sub_view="plan", strategy_id=strategy_id
            ).pack()
        ))

    if watchlist_removable:
        builder.row(types.InlineKeyboardButton(
            text="🗑 Убрать из СН",
            callback_data=MenuAction(
                action="confirm_remove_wl", portfolio_id=portfolio_id, listing_id=listing_id, ticker_name=symbol
            ).pack()
        ))

    nav_kb = generate_nav_back_keyboard(one_step_back_text=back_text, full_back_callback=back_callback)
    builder.attach(InlineKeyboardBuilder.from_markup(nav_kb))
    return builder.as_markup()

def generate_main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Генерирует интерактивный пульт Главного меню экосистемы UPort.
    Проверяет гибкий флаг админа для отображения скрытых инженерных настроек.
    """
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Общая сводка капитала", callback_data=MenuAction(action="show_summary").pack()))
    builder.row(types.InlineKeyboardButton(text="🔬 Списки наблюдения", callback_data=MenuAction(action="show_watchlist_focus").pack()))
    builder.row(types.InlineKeyboardButton(text="📅 Утренний дайджест", callback_data=MenuAction(action="show_digest_focus").pack()))
    builder.row(types.InlineKeyboardButton(text="🔄 Обновить цены рынка", callback_data=MenuAction(action="update_prices").pack()))

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

def build_name_block(name: str, width: int = 24) -> str:
    """
    Микро-станок 2б: Жесткий блок фиксированной ширины для произвольного текста
    (например, названия стратегии). В отличие от тикера, ширина текста не задана
    доменной моделью -- поэтому она параметризована, а слишком длинные имена
    обрезаются с многоточием, чтобы не сломать выравнивание колонок соседних блоков.
    """
    clean_name = str(name).strip()
    if len(clean_name) > width:
        clean_name = clean_name[:width - 1].rstrip() + "…"
    padding_count = max(0, width - len(clean_name))
    return f"{clean_name}{EN_SPACE * padding_count}{BRAILLE_EMPTY}"

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
        elif b_type == "name":
            final_string += build_name_block(name=block.get("value", ""), width=int(block.get("width", 24)))
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

def generate_strategy_button_text(name: str, target_pct: float, actual_pct: float, name_width: int = 14, icon: str = "🎯") -> str:
    """
    Генерирует жесткую монолитную строку для инлайн-кнопок списка стратегий портфеля.
    Название стратегии -- произвольной длины (не тикер), поэтому идёт через build_name_block;
    план/факт доли всё равно выравниваются в колонки, как и в остальных экранах UPort.
    name_width по умолчанию уменьшен под короткие однословные подписи (см.
    bot_handlers/portfolios.py:SHORT_STRATEGY_LABELS) -- полное имя стратегии в кнопке
    не помещалось на экране телефона и обрезалось Telegram-клиентом.
    icon -- по умолчанию 🎯, но пассивная стратегия (см. Claude/BACKLOG.md п.9,
    2026-07-31) передаёт 😴 -- отдельный слот, не откусывает у name_width.
    """
    blueprint = [
        {"type": "badge", "icon": icon, "index": 0},
        {"type": "name", "value": name, "width": name_width},
        {"type": "percent", "value": target_pct, "show_sign": False},
        {"type": "separator", "value": "/"},
        {"type": "percent", "value": actual_pct, "show_sign": False},
        {"type": "final_row", "value": ""}
    ]
    return assemble_lego_line(blueprint)

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

def generate_watchlist_button_text(
    ticker: str, f1: str, f2: str, f3: str, f4: str,
    alert_icon: str, alerts_count: int, f_strategy: str = "", strategy_count: int = 0
) -> str:
    """
    Генерирует жесткую монолитную строку для инлайн-кнопок Списков Наблюдения.
    Многомерный радар, каждое знакоместо управляется независимо. Фазы "Изучение"
    (considered_at) и "Наблюдение" (watched_at) убраны из радара 2026-07-30 --
    см. BACKLOG.md п.12/47 (watched_at заполнен всегда, considered_at не пишется
    ни одним живым интерактивным путём).
    f4 -- ортогонален жизненному циклу f1-f3 (не фаза "ордер→портфель→распродано", а
    отдельный факт "прямо сейчас есть активный План" -- order_pipelines PENDING/ACTIVE,
    см. Claude/11_asset_lifecycle_and_plan.md). Стоит ПОСЛЕ колокольчика алертов (не
    внутри кластера жизненного цикла) -- по просьбе пользователя 2026-07-29, визуально
    отдельная колонка правее.
    f_strategy/strategy_count -- в скольких стратегиях портфеля бумага реально держится
    сейчас (strategy_assets.allocated_quantity > 0), число, не просто факт "да/нет" --
    вставлено между "Портфель" и "Распродано" по просьбе пользователя 2026-07-30.
    """
    blueprint = [
        {"type": "ticker", "value": ticker},
        {"type": "separator", "value": " "},
        {"type": "badge", "icon": f1, "index": 0},          # Ордер (📃)
        {"type": "badge", "icon": f2, "index": 0},          # Портфель (💼)
        {"type": "badge", "icon": f_strategy, "index": strategy_count},  # В скольких стратегиях портфеля (🎯N)
        {"type": "badge", "icon": f3, "index": 0},          # Распродано (🏁)
        {"type": "badge", "icon": alert_icon, "index": alerts_count},  # Колокольчик алертов (Управляется из хэндлера)
        {"type": "badge", "icon": f4, "index": 0},          # Есть активный План (📋), правее колокольчика
        {"type": "final_row", "value": ""}
    ]
    return assemble_lego_line(blueprint)

def generate_digest_toc_keyboard(portfolio_id: int, sections: dict):
    """
    Оглавление свёрнутого дайджеста (см. Claude/BACKLOG.md п.35) -- одна кнопка на
    непустой раздел, с счётчиком. Пустые разделы не показываются (как и в тексте).
    Переиспользует generate_tab_switch_keyboard -- дайджест-разделы это те же вкладки
    одного экрана. Возвращает None, если действий сегодня вообще нет.
    """
    from analytics.daily_digest import SECTION_ORDER

    tabs = []
    for key in SECTION_ORDER:
        sec = sections[key]
        if not sec["items"]:
            continue
        tabs.append((
            f"{sec['emoji']} {sec['label']} ({len(sec['items'])})",
            MenuAction(action="view_digest", portfolio_id=portfolio_id, sub_view=key)
        ))

    if not tabs:
        return None
    return generate_tab_switch_keyboard(tabs, current_sub_view="overview")

def generate_digest_section_keyboard(portfolio_id: int, section_key: str, items: list):
    """
    Клавиатура детального раздела дайджеста -- одна кнопка-действие на пункт (см.
    Claude/BACKLOG.md п.35 для разбора, какое действие у какого раздела):
    "listing_id" в пункте -- открыть карточку тикера (бумага уже держится/есть приказ);
    "ticker_id" без "listing_id" -- кандидат на покупку, кнопка "В список наблюдения"
    (заводит листинг сама, если его ещё нет); ни того ни другого -- заглушка "в
    разработке" (целевой экран для этого раздела ещё не решён). Плюс навигация назад
    к свёрнутому дайджесту.

    sub_view="digest" в callback "confirm_wl_add" -- составной sub_view с пометкой
    происхождения (тот же приём, что уже решил ту же природу проблемы для шторки
    алертов, см. BACKLOG.md "Сделано" п.17): экран успеха добавления в список
    наблюдения (bot_handlers/watchlist.py::execute_watchlist_fixation) по этой
    пометке вернёт "🔙 Назад к дайджесту", а не только "В главное меню".
    """
    builder = InlineKeyboardBuilder()
    for item in items:
        if item.get("listing_id"):
            builder.row(types.InlineKeyboardButton(
                text=f"🔗 {item['label']}",
                callback_data=MenuAction(
                    action="view_ticker", portfolio_id=portfolio_id, listing_id=int(item["listing_id"]), sub_view="owner"
                ).pack()
            ))
        elif item.get("ticker_id"):
            builder.row(types.InlineKeyboardButton(
                text=f"🔬 В список наблюдения: {item['label']}",
                callback_data=MenuAction(
                    action="confirm_wl_add", portfolio_id=portfolio_id, ticker_id=int(item["ticker_id"]), sub_view="digest"
                ).pack()
            ))
        else:
            builder.row(types.InlineKeyboardButton(
                text=f"🔧 {item['label']} (в разработке)" if item.get("label") else "🔧 В разработке",
                callback_data=MenuAction(action="digest_stub").pack()
            ))

    final_builder = InlineKeyboardBuilder.from_markup(builder.as_markup())
    back_kb = generate_nav_back_keyboard(
        one_step_back_text="🔙 Назад к дайджесту",
        full_back_callback=MenuAction(action="view_digest", portfolio_id=portfolio_id, sub_view="overview").pack()
    )
    final_builder.attach(InlineKeyboardBuilder.from_markup(back_kb))
    return final_builder.as_markup()
