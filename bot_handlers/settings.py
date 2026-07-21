import asyncio
import logging
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем шлюз СУБД, фабрику и модуль ночного Yahoo-воркера
from database import db_sys
from bot_handlers.common import MenuAction
from site_connectors.sync_fundamentals_yhoo import sync_fundamentals

# Инициализируем роутер инженерного контура настроек
router = Router()

@router.callback_query(MenuAction.filter(F.action == "settings_main"))
async def process_settings_main(callback: types.CallbackQuery, state: FSMContext):
    """Шторка Уровня 1: Скрытая панель администратора системы UPort."""
    # Жесткая проверка безопасности: считываем флаг админа из памяти FSM
    user_data = await state.get_data()
    if not user_data.get("is_admin", False):
        await callback.answer("🛑 Отказано в доступе: контур заблокирован.", show_alert=True)
        return

    await callback.answer()
    
    text = (
        "⚙️ **Панель администратора UPort**\n"
        "Добро пожаловать в инженерный пульт управления.\n"
        "Внимание: действия ниже запускают тяжелые системные процессы напрямую в СУБД!\n"
        "───────────────────\n"
        "Вы можете принудительно обновить текстовые паспорта акций (сектора и индустрии), "
        "а также скачать 18 актуальных фундаментальных мультипликаторов с Wall Street через Yahoo Finance."
    )

    builder = InlineKeyboardBuilder()
    # Кнопка принудительного пинка Yahoo-контура
    builder.row(types.InlineKeyboardButton(
        text="📊 Обновить фундаментал (Yahoo)",
        callback_data=MenuAction(action="run_yahoo_sync").pack()
    ))
    builder.row(types.InlineKeyboardButton(
        text="➕ Создать портфель",
        callback_data=MenuAction(action="admin_portfolio_new").pack()
    ))
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@router.callback_query(MenuAction.filter(F.action == "run_yahoo_sync"))
async def process_yahoo_sync_callback(callback: types.CallbackQuery, state: FSMContext):
    """Реакция на кнопку: запуск ночного воркера Yahoo в изолированном фоновом потоке."""
    user_data = await state.get_data()
    if not user_data.get("is_admin", False):
        await callback.answer("🛑 Отказано в доступе.", show_alert=True)
        return

    # Мгновенно убираем песочные часы в Telegram, чтобы интерфейс не зависал
    await callback.answer("🚀 Запускаю фоновый анализ рынка...")
    
    # Меняем текст шторки, показывая инвестору, что процесс пошел
    await callback.message.edit_text(
        "⏳ **Системный статус: Выполняется анализ...**\n\n"
        "🤖 Робот UPort подключился к серверам Yahoo Finance.\n"
        "Скачиваю сектора, индустрии и 18 экономических мультипликаторов стоимости для всех отслеживаемых семьей акций.\n\n"
        "ℹ️ *Вы можете закрыть это окно или вернуться в Главное меню — процесс идет асинхронно на сервере DigitalOcean.*",
        parse_mode="Markdown",
        reply_markup=builder_back_only()
    )

    # Внутренняя изолированная функция для потока
    def background_sync_task():
        logging.info("🧠 [Инженерный Пульт]: Ручной принудительный запуск sync_fundamentals...")
        try:
            # Вызываем наш исправленный модуль фундаментала под максимальными правами db_sys
            sync_fundamentals(db_sys)
            logging.info("🧠 [Инженерный Пульт]: Ручной запуск sync_fundamentals завершен успешно.")
            return True
        except Exception as err:
            logging.error(f"❌ [Инженерный Пульт CRITICAL ERROR]: Сбой ручного синка Yahoo: {err}")
            return False

    # Асинхронный хук: отправляем тяжелую задачу в пул потоков ОС
    async def run_and_report():
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, background_sync_task)
        
        # 🔥 ИСПРАВЛЕНИЕ: Вместо отправки нового сообщения, ювелирно РЕДАКТИРУЕМ старое на месте!
        try:
            if success:
                await callback.message.edit_text(
                    text="✅ **Анализ Wall Street завершен успешно!**\nВсе сектора, индустрии и фундаментальные коэффициенты акций актуализированы в `public.tickers`.",
                    parse_mode="Markdown",
                    reply_markup=builder_back_only() # Возвращаем кнопку главного меню
                )
            else:
                await callback.message.edit_text(
                    text="❌ **Сбой анализа рынка!**\nПроизошла ошибка при скачивании данных с Yahoo Finance. Проверьте лог `uport_system.log`.",
                    parse_mode="Markdown",
                    reply_markup=builder_back_only()
                )
        except Exception as send_err:
            # На случай, если пользователь за эти 30 секунд уже ушел в другое меню и закрыл окно
            print(f"Не удалось отредактировать статус-сообщение: {send_err}")

    # Запускаем фоновую корутину без await, чтобы она крутилась независимо
    asyncio.create_task(run_and_report())


def builder_back_only() -> types.InlineKeyboardMarkup:
    """Вспомогательная кнопка возврата во время выполнения задачи."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))
    return builder.as_markup()
