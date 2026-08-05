#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.utils.keyboard import InlineKeyboardBuilder

sys.path.append(str(Path(__file__).parent.resolve()))
from bot_handlers.bot_keyboards import build_smart_badge

# Настраиваем вывод логов в консоль
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# 🔥 ВАШ ТЕЛЕГРАМ ID ДЛЯ ПОЛУЧЕНИЯ КНОПОК
TARGET_USER_ID = 250720161

# Системные константы калибровки Юникода
EN_SPACE = "\u2002"       # Фиксированный пробел
FIGURE_SPACE = "\u2007"   # Цифровой пробел (равен ширине одной цифры)
BRAILLE_EMPTY = "\u2800"  # Прозрачный знак Брайля
SIX_PER_EM = "\u2006"     # Тончайший микро-отступ (1/6 пробела)


# =========================================================================
# ⚙️ МИКРО-СТАНК ТЕКСТОВЫХ ТИКЕРОВ (Откалиброван на Шаге 2)
# =========================================================================

def build_ticker_block(ticker: str) -> str:
    """
    Превращает любой тикер в жесткий текстовый блок фиксированной ширины.
    Выносит тикер влево, добивает en_space до 5 знаков + Брайль-заглушка.
    """
    clean_ticker = str(ticker).strip().upper()
    padding_count = max(0, 5 - len(clean_ticker))
    return f"{clean_ticker}{EN_SPACE * padding_count}"


# =========================================================================
# ⚙️ МИКРО-СТАНК ЧИСЕЛ И ОБЪЕМОВ (Новое внедрение Шага 3)
# =========================================================================

def build_number_block(value: int, suffix: str = "шт", show_sign: bool = False) -> str:
    """
    Бухгалтерское форматирование целых чисел и дельт (DOS/Asm Стандарт).
    🔥 ИСПРАВЛЕНО: Знак числа приклеивается вплотную к первой живой цифре!
    """
    # 1. Извлекаем чистый математический знак
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    abs_value = abs(value)
    raw_qty_str = f"{int(abs_value):04d}" # Базовый шаблон на 4 цифры
    
    # 2. Находим индекс первой значащей (не нулевой) цифры
    first_live_idx = 3 # По умолчанию, если число = 0, значащий последний ноль
    for idx, char in enumerate(raw_qty_str):
        if char != '0':
            first_live_idx = idx
            break

    # 3. Собираем строку из 5 знакомест (1 под знак, 4 под цифры)
    final_block = ""
    
    # Идем по структуре шаблона с учетом выделенного места под знак
    # Нам нужно распределить лидирующие пробелы, прижатый знак и цифры
    needed_paddings = first_live_idx
    if sign_char == "":
        # Если знака вообще нет (просто число лотов), заполняем левую пустую позицию знаком-призраком
        final_block += FIGURE_SPACE
        final_block += FIGURE_SPACE * needed_paddings
    else:
        # Если знак есть, он съедает одно левое знакоместо, сдвигаясь к первой цифре
        final_block += FIGURE_SPACE * needed_paddings
        final_block += sign_char

    # Дописываем оставшиеся живые цифры числа
    final_block += raw_qty_str[first_live_idx:]
            
    return f"{final_block}{suffix}"

# =========================================================================
# ⚙️ МИКРО-СТАНК ВАЛЮТЫ И ЦЕН (Новое внедрение Шага 4)
# =========================================================================

def build_finance_block(value: float, currency_symbol: str = "$", show_sign: bool = False) -> str:
    """
    Сверхточный финансовый кубик по DOS-сетке (Знак + Валюта + 4 знака целых + Точка + 2 знака копеек).
    🔥 Знак и символ валюты приклеиваются вплотную к первой живой цифре числа!
    """
    # 1. Извлекаем знак числа
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    # 2. Форматируем числовую часть в жесткий шаблон целой части (0000.00)
    abs_value = abs(value)
    raw_finance_str = f"{abs_value:07.2f}" # 4 знака до точки, 1 точка, 2 знака после = 7 символов всего
    
    # Разделяем целую часть для поиска лидирующих нулей
    parts = raw_finance_str.split(".")
    int_part = parts[0]
    dec_part = parts[1]

    # Находим индекс первой живой цифры в целой части
    first_live_idx = 3 # По умолчанию, если целая часть = 0 (например, 0.50), значащий последний ноль
    for idx, char in enumerate(int_part):
        if char != '0':
            first_live_idx = idx
            break

    # 3. Собираем финансовый кирпич (Резервируем 1 место под знак, 1 под валюту, 4 под целое)
    final_block = ""
    needed_paddings = first_live_idx

    if sign_char == "":
        # Знака нет: резервируем левое место знаком-призраком, сдвигаем валюту к числу
        final_block += FIGURE_SPACE  # Место под отсутствующий знак
        final_block += FIGURE_SPACE * needed_paddings
        final_block += currency_symbol
    else:
        # Знак есть: он съедает одно левое знакоместо, сдвигая связку [Знак][Валюта] к числу
        final_block += FIGURE_SPACE * needed_paddings
        final_block += sign_char + currency_symbol

    # Дописываем оставшуюся живую целую часть, точку и копейки
    final_block += int_part[first_live_idx:] + "." + dec_part
    return final_block

# =========================================================================
# ⚙️ МИКРО-СТАНК ПРОЦЕНТОВ
# =========================================================================

def build_percent_block(value: float, show_sign: bool = False) -> str:
    """
    ⚙️ МИКРО-СТАНК ПРОЦЕНТОВ (Внедрение Шага 5)
    Форматирует проценты по жесткой сетке: Знак + 2 знака целых + Точка + 2 знака дробных + %.
    Знак приклеивается вплотную к первой живой цифре числа.
    """
    # 1. Извлекаем знак
    sign_char = ""
    if value < 0:
        sign_char = "-"
    elif value > 0 and show_sign:
        sign_char = "+"

    # 2. Шаблон на 2 знака целых, точку и 2 знака дробных (всего 5 символов: 00.00)
    abs_value = abs(value)
    raw_pct_str = f"{abs_value:05.2f}"
    
    parts = raw_pct_str.split(".")
    int_part = parts[0]
    dec_part = parts[1]

    # Ищем первую живую цифру (максимум индекс 1, так как целых всего 2 знака)
    first_live_idx = 1
    for idx, char in enumerate(int_part):
        if char != '0':
            first_live_idx = idx
            break

    # 3. Сборка (Резервируем 1 место под знак, 2 под целое)
    final_block = ""
    needed_paddings = first_live_idx

    if sign_char == "":
        final_block += FIGURE_SPACE + (FIGURE_SPACE * needed_paddings)
    else:
        final_block += (FIGURE_SPACE * needed_paddings) + sign_char

    final_block += int_part[first_live_idx:] + "." + dec_part + "%"
    return final_block

# =========================================================================
# ⚙️ МИКРО-СТАНК РАЗДЕЛИТЕЛЕЙ
# =========================================================================

def build_separator_block(char: str) -> str:
    """
    ⚙️ МИКРО-СТАНК РАЗДЕЛИТЕЛЕЙ (Внедрение Шага 6)
    Генерирует фиксированный по ширине разделитель.
    Оборачивает символ (или пробел) в невидимые воздушные подушки Брайля.
    """    
    # Очищаем входящий символ от случайных пробелов
    clean_char = str(char).strip()
    
    # Если на входе пустая строка или обычный пробел — выдаем чистый воздух фиксированной ширины
    if clean_char == "" or clean_char == " ":
        return f"{BRAILLE_EMPTY}{BRAILLE_EMPTY}"
        
    # Если передан символ (/, *, -, • и т.д.), зажимаем его между двумя Брайль-знаками
    return f"{BRAILLE_EMPTY}{clean_char}{BRAILLE_EMPTY}"

# =========================================================================
# 🏗️ ПУНКТ 2: СБОРOЧНЫЙ КОНВЕЙЕР (Упорядоченный Лего-сборщик)
# =========================================================================

def assemble_lego_line(json_blueprint: list) -> str:
    """
    Конвейер сборки. Склеивает радар, тикеры и объемы штук.
    """
    final_string = ""
    for block in json_blueprint:
        b_type = block.get("type")
        
        if b_type == "badge":
            final_string += build_smart_badge(
                icon=block.get("icon", ""), 
                value=int(block.get("index", 0))
            )
        elif b_type == "ticker":
            final_string += build_ticker_block(
                ticker=block.get("value", "")
            )
        elif b_type == "number":
            # Вызываем наш новый модернизированный микро-станок чисел
            final_string += build_number_block(
                value=int(block.get("value", 0)),
                suffix=block.get("suffix", "шт"),
                show_sign=bool(block.get("show_sign", False))
            )
        elif b_type == "finance":
            # Вызываем наш новый финансовый микро-станок
            final_string += build_finance_block(
                value=float(block.get("value", 0.0)),
                currency_symbol=block.get("currency_symbol", "$"),
                show_sign=bool(block.get("show_sign", False))
            )
        elif b_type == "percent":
            # Вызываем новый процентный микро-станок
            final_string += build_percent_block(
                value=float(block.get("value", 0.0)),
                show_sign=bool(block.get("show_sign", False))
            )
        elif b_type == "separator":
            # Вызываем новый микро-станок разделителей
            final_string += build_separator_block(
                char=block.get("value", "")
            )
        elif b_type == "final_row":
            block["value"] = final_string
            
    return final_string


# =========================================================================
# 📱 ПУНКТ 3: ГЕНЕРАТОР КЛАВИАТУРЫ И ТЕСТЕР ДЛЯ ШАГА 3
# =========================================================================

async def run_render_test():
    print("🧪 [UPORT RENDERER]: Запуск Шага 3 — Калибровка бухгалтерской колонки штук...")
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not token:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_TOKEN не найден в .env")
        return
        
    bot = Bot(token=token)
    builder = InlineKeyboardBuilder()

    # СТРОИМ МАТРИЦУ ИЗ 6 КНОПОК С КРИТИЧЕСКИМИ ОБЪЕМАМИ
    # Контур: Иконка-шарик (индекс 0) + Тикер + Объемы штук
    lego_matrix = [
        # Кнопка 1: MU, 10шт, цена 145.20$, процент +4.20%
        [
            {"type": "badge", "icon": "🟢", "index": 1},
            {"type": "ticker", "value": "MU"},
            {"type": "number", "value": 10, "suffix": "шт", "show_sign": False},
            {"type": "finance", "value": 145.20, "currency_symbol": "$", "show_sign": False},
            {"type": "percent", "value": 4.20, "show_sign": True},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 2: BRK.B, дельта -45шт, убыток -$34.10, процент -1.15%
        [
            {"type": "badge", "icon": "🟢", "index": 0},
            {"type": "ticker", "value": "BRK.B"},
            {"type": "number", "value": -45, "suffix": "шт", "show_sign": True},
            {"type": "finance", "value": -34.10, "currency_symbol": "$", "show_sign": True},
            {"type": "percent", "value": -1.15, "show_sign": True},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 3: NOW, дельта +1250шт, приток +1520.45$, процент +12.50%
        [
            {"type": "badge", "icon": "🟢", "index": 3},
            {"type": "ticker", "value": "NOW"},
            {"type": "number", "value": 1250, "suffix": "шт", "show_sign": True},
            {"type": "finance", "value": 1520.45, "currency_symbol": "$", "show_sign": True},
            {"type": "percent", "value": 12.50, "show_sign": True},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 4: NVDA, 5шт, цена 0.50$, процент +0.05%
        [
            {"type": "badge", "icon": "🟢", "index": 0},
            {"type": "ticker", "value": "NVDA"},
            {"type": "number", "value": 5, "suffix": "шт", "show_sign": False},
            {"type": "finance", "value": 0.50, "currency_symbol": "$", "show_sign": False},
            {"type": "percent", "value": 0.05, "show_sign": True},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 5: V, 0шт, дельта 0.00$, процент 0.00%
        [
            {"type": "badge", "icon": "🟢", "index": 9},
            {"type": "ticker", "value": "V"},
            {"type": "number", "value": 0, "suffix": "шт", "show_sign": False},
            {"type": "finance", "value": 0.00, "currency_symbol": "$", "show_sign": True},
            {"type": "percent", "value": 0.00, "show_sign": True},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 6: GLDM, дельта -99шт, убыток -999.00£, процент -99.99%
        [
            {"type": "badge", "icon": "🟢", "index": 0},
            {"type": "ticker", "value": "GLDM"},
            {"type": "number", "value": -99, "suffix": "шт", "show_sign": True},
            {"type": "finance", "value": -999.00, "currency_symbol": "£", "show_sign": True},
            {"type": "percent", "value": -99.99, "show_sign": True},
            {"type": "final_row", "value": ""}
        ]
    ]
    
    try:
        for row_blueprint in lego_matrix:
            assembled_line = assemble_lego_line(row_blueprint)
            button_text = f"{assembled_line}"
            builder.row(types.InlineKeyboardButton(text=button_text, callback_data="calib_click"))

        msg_text = (
            "⚙️ **ШАГ 3: КАЛИБРОВКА БУХГАЛТЕРСКИХ ОБЪЕМОВ**\n\n"
            "Внимательно оцените выравнивание числовой колонки штук:\n"
            "1. Встали ли суффиксы `шт` и правые палочки `|` по ровной вертикальной струнке?\n"
            "2. Удерживает ли `Figure Space` левый край штук при наличии знаков `+` и `-`?\n\n"
            "*Если правая граница стоит как влитая, значит Шаг 3 выполнен успешно!*"
        )

        await bot.send_message(chat_id=TARGET_USER_ID, text=msg_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        print(f"🚀 Калибровочный пульт Шага 3 успешно отправлен в ваш Telegram. Проверяйте!")

    except Exception as e:
        print(f"❌ Критический сбой тестера: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(run_render_test())
