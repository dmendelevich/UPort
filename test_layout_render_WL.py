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

# 🔥 ВАШ РЕАЛЬНЫЙ ТЕЛЕГРАМ ID ДЛЯ ПОЛУЧЕНИЯ КНОПОК
TARGET_USER_ID = 250720161

# Системные константы калибровки Юникода
EN_SPACE = "\u2002"       # Фиксированный пробел
BRAILLE_EMPTY = "\u2800"  # Прозрачный знак Брайля
SIX_PER_EM = "\u2006"     # Тончайший микро-отступ (1/6 пробела)


# =========================================================================
# ⚙️ МИКРО-СТАНК ТЕКСТОВЫХ ТИКЕРОВ (Новое внедрение Шага 2)
# =========================================================================

def build_ticker_block(ticker: str) -> str:
    """
    Превращает любой тикер в жесткий текстовый блок фиксированной ширины.
    Добивает en_space до эталона в 5 символов (под размер BRK.B).
    """
    clean_ticker = str(ticker).strip().upper()
    padding_count = max(0, 5 - len(clean_ticker))
    return f"{clean_ticker}{EN_SPACE * padding_count}{BRAILLE_EMPTY}{BRAILLE_EMPTY}"

# =========================================================================
# ⚙️ МИКРО-СТАНК РАЗДЕЛИТЕЛЕЙ
# =========================================================================

def build_separator_block(char: str) -> str:
    """
    ⚙️ МИКРО-СТАНК РАЗДЕЛИТЕЛЕЙ (Внедрение Шага 6)
    Генерирует фиксированный по ширине разделитель.
    Оборачивает символ (или пробел) в невидимые воздушные подушки Брайля.
    """
    BRAILLE_EMPTY = "\u2800"  # Прозрачный знак Брайля
    
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
    Конвейер сборки. Склеивает графические блоки и текстовые тикеры.
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
            # Вызываем наш новый микро-станок тикеров
            final_string += build_ticker_block(
                ticker=block.get("value", "")
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
# 📱 ПУНКТ 3: ГЕНЕРАТОР КЛАВИАТУРЫ И ТЕСТЕР ДЛЯ ШАГА 2
# =========================================================================

async def run_render_test():
    print("🧪 [UPORT RENDERER]: Запуск Шага 2 — Калибровка блока тикеров разной длины...")
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not token:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_TOKEN не найден в .env")
        return
        
    bot = Bot(token=token)
    builder = InlineKeyboardBuilder()

    # СТРОИМ ДВУХУРОВНЕВЫЙ JSON-ЧЕРТЕЖ С ТИКЕРАМИ ДЛЯ 6 КНОПОК
    # К идеальному радар-блоку припаиваем тикеры разной длины
    lego_matrix = [
        # Кнопка 1: Полный радар + Короткий тикер MU (2 знака)
        [
            {"type": "ticker", "value": "MU"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "🔍", "index": 1},
            {"type": "badge", "icon": "🎯", "index": 2},
            {"type": "badge", "icon": "📃", "index": 3},
            {"type": "badge", "icon": "💼", "index": 4},
            {"type": "badge", "icon": "🏁", "index": 5},
            {"type": "badge", "icon": "🔔", "index": 6},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 2: Проверка перегрузки + Длинный составной тикер BRK.B (5 знаков)
        [
            {"type": "ticker", "value": "BRK.B"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "🔍", "index": 12},
            {"type": "badge", "icon": "🎯", "index": 15},
            {"type": "badge", "icon": "📃", "index": 20},
            {"type": "badge", "icon": "💼", "index": 11},
            {"type": "badge", "icon": "🏁", "index": 99},
            {"type": "badge", "icon": "🔔", "index": 14},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 3: Шахматный радар + Тикер NOW (3 знака)
        [
            {"type": "ticker", "value": "NOW"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "🔍", "index": 1},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "📃", "index": 3},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "🏁", "index": 5},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 4: Только колокольчик + Тикер NVDA (4 знака)
        [
            {"type": "ticker", "value": "NVDA"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "🔔", "index": 7},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 5: Внутренний шахматный сдвиг + Ультра-короткий тикер V (1 знак)
        [
            {"type": "ticker", "value": "V"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "🎯", "index": 2},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "💼", "index": 4},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "🔔", "index": 9},
            {"type": "final_row", "value": ""}
        ],
        # Кнопка 6: Абсолютно пустой радар + Экзотический тикер GLDM (4 знака)
        [
            {"type": "ticker", "value": "GLDM"},
            {"type": "separator", "value": " "},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "badge", "icon": "", "index": 0},
            {"type": "final_row", "value": ""}
        ]
    ]

    try:
        # Пропускаем каждую строчку через конвейер сборки Шага 2
        for row_blueprint in lego_matrix:
            assembled_line = assemble_lego_line(row_blueprint)
            button_text = f"|{assembled_line}|"
            builder.row(types.InlineKeyboardButton(text=button_text, callback_data="calib_click"))

        msg_text = (
            "⚙️ **ШАГ 2: КАЛИБРОВКА ТИКЕРОВ РАЗНОЙ ДЛИНЫ**\n\n"
            "Внимательно посмотрите на правые палочки `|`:\n"
            "1. Удержал ли `en_space` геометрию при тикерах от 1 до 5 знаков?\n"
            "2. Выстроились ли правые палочки `|` в ровную вертикальную линию под тикером `BRK.B`?\n\n"
            "*Если края идеально ровные, значит тикерный блок откалиброван!*"
        )

        await bot.send_message(chat_id=TARGET_USER_ID, text=msg_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        print(f"🚀 Калибровочный пульт Шага 2 успешно отправлен в ваш Telegram. Проверяйте!")

    except Exception as e:
        print(f"❌ Критический сбой тестера: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(run_render_test())
