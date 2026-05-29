import asyncio
import sys
from aiogram import Router, types, F, Bot
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# Импортируем готовые объекты из ядра и донора
from database import db_bot
from bot_handlers.common import MenuAction

router = Router()

class BacklogStates(StatesGroup):
    waiting_for_task_text = State()


# --- ВСПОМОГАТЕЛЬНЫЙ УНИВЕРСАЛЬНЫЙ РЕНДЕРИНГ ЭКРАНА БЭКЛОГА ---

async def render_backlog_screen(target_message: types.Message, state: FSMContext):
    """
    Базовая изолированная функция отрисовки бэклога.
    Принимает конкретное сообщение, которое нужно отредактировать.
    """
    print("🔍 [БЭКЛОГ]: Отправляю SQL-запрос на выборку невыполненных задач...")
    sql_get_tasks = "SELECT id, title, description, priority, status FROM public.backlog WHERE status != 'done' ORDER BY priority ASC, id ASC;"
    
    try:
        tasks = await asyncio.to_thread(db_bot.execute_query, sql_get_tasks)
        if not isinstance(tasks, list):
            tasks = [tasks] if tasks else []
        print(f"✅ [БЭКЛОГ]: Из СУБД успешно извлечено задач: {len(tasks)}")
    except Exception as db_err:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА БЭКЛОГА]: Сбой SELECT-запроса к СУБД: {db_err}")
        tasks = []

    p1_lines, p2_lines, p3_lines = [], [], []
    builder = InlineKeyboardBuilder()
    
    print("⚙️ [БЭКЛОГ]: Начало цикла сборки динамических инлайн-кнопок закрытия...")
    for t in tasks:
        t_id = int(t['id'])
        title = t['title']
        desc = t['description'] or "Без описания"
        prio = int(t['priority'])
        
        task_block = f"**#{t_id}. {title}**\n└ _Суть:_ {desc}\n"
        
        if prio == 1:
            p1_lines.append(task_block)
            builder.row(types.InlineKeyboardButton(text=f"🔴 Закрыть #{t_id}", callback_data=MenuAction(action="backlog_close", task_id=t_id).pack()))
        elif prio == 3:
            p3_lines.append(task_block)
            builder.row(types.InlineKeyboardButton(text=f"🟢 Закрыть #{t_id}", callback_data=MenuAction(action="backlog_close", task_id=t_id).pack()))
        else:
            p2_lines.append(task_block)
            builder.row(types.InlineKeyboardButton(text=f"🟡 Закрыть #{t_id}", callback_data=MenuAction(action="backlog_close", task_id=t_id).pack()))

    report_text = "🛠️ **Интерактивный Бэклог UPort**\n"
    report_text += "Управляйте задачами архитектуры прямо с телефона:\n"
    report_text += "───────────────────\n"
    
    if p1_lines:
        report_text += "🚨 **КРИТИЧЕСКИЙ ПРИОРИТЕТ (Priority 1):**\n" + "\n".join(p1_lines) + "───────────────────\n"
    if p2_lines:
        report_text += "⚡ **СРЕДНИЙ ПРИОРИТЕТ (Priority 2):**\n" + "\n".join(p2_lines) + "───────────────────\n"
    if p3_lines:
        report_text += "🏕️ **НИЗКИЙ ПРИОРИТЕТ / ПРИВАЛ (Priority 3):**\n" + "\n".join(p3_lines) + "───────────────────\n"
        
    if not tasks:
        report_text += " 🎉 *Все архитектурные задачи выполнены! Костыли отсутствуют, система в идеальном порядке.*\n───────────────────\n"

    report_text += f"📊 Всего активных задач в СУБД: **{len(tasks)}**"

    builder.row(
        types.InlineKeyboardButton(text="➕ Добавить задачу", callback_data=MenuAction(action="backlog_add_mode").pack()),
        types.InlineKeyboardButton(text="🧹 Очистить архив", callback_data=MenuAction(action="backlog_clear_done").pack())
    )
    builder.row(types.InlineKeyboardButton(text="📱 В главное меню", callback_data=MenuAction(action="main_menu").pack()))

    print("🖥️ [БЭКЛОГ]: Рендеринг шторки завершен. Отправляю edit_text в Telegram...")
    try:
        await target_message.edit_text(report_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        print("🎉 [БЭКЛОГ УСПЕХ]: Экран бэклога успешно обновлен на телефоне./n")
    except TelegramBadRequest:
        # Игнорируем ошибку, если текст сообщения не изменился
        pass
    except Exception as tg_err:
        print(f"⚠️ [БЭКЛОГ WARNING]: Непредвиденная ошибка отрисовки: {tg_err}")


# --- ХЭНДЛЕРЫ ИНТЕРФЕЙСА БЭКЛОГА ---

@router.callback_query(MenuAction.filter(F.action == "backlog_main"))
async def process_backlog_main(callback: types.CallbackQuery, state: FSMContext):
    """Шторка Бэклога: Извлекает задачи из public.backlog (Вызов через Кнопку)."""
    print(f"\n📥 [БЭКЛОГ ТРИГГЕР]: Пойман клик от пользователя {callback.from_user.id} ({callback.from_user.full_name})")
    print("🧠 [БЭКЛОГ]: Сбрасываю любые старые FSM состояния...")
    await state.clear()
    await callback.answer("Загружаю бэклог...")
    
    await render_backlog_screen(callback.message, state)


@router.callback_query(MenuAction.filter(F.action == "backlog_close"))
async def process_backlog_close(callback: types.CallbackQuery, callback_data: MenuAction, state: FSMContext):
    """Мгновенный UPDATE статуса задачи в СУБД в один клик."""
    t_id = callback_data.task_id
    print(f"\n🎯 [БЭКЛОГ ЭКШЕН]: Получена команда закрытия задачи #{t_id}")
    await callback.answer(f"Задача #{t_id} выполнена! 🎉")
    
    sql_update = f"UPDATE public.backlog SET status = 'done' WHERE id = {t_id};"
    print(f"🗄️ [БЭКЛОГ]: Выполняю запрос: {sql_update}")
    await asyncio.to_thread(db_bot.execute_query, sql_update)
    
    print("♻️ [БЭКЛОГ]: Перенаправляю поток на обновление главного экрана бэклога...")
    await render_backlog_screen(callback.message, state)


@router.callback_query(MenuAction.filter(F.action == "backlog_clear_done"))
async def process_backlog_clear_done(callback: types.CallbackQuery, state: FSMContext):
    """Полное удаление закрытых задач из архива таблицы."""
    print("\n🧹 [БЭКЛОГ ЭКШЕН]: Получен приказ на полную очистку архива выполненных задач...")
    await callback.answer("Архив очищен.")
    
    sql_delete = "DELETE FROM public.backlog WHERE status = 'done';"
    print(f"🗄️ [БЭКЛОГ]: Выполняю запрос: {sql_delete}")
    await asyncio.to_thread(db_bot.execute_query, sql_delete)
    
    await render_backlog_screen(callback.message, state)


# --- РЕЖИМ ИНЖЕКЦИИ ЗАДАЧ ТЕКСТОМ (FSM) ---

@router.callback_query(MenuAction.filter(F.action == "backlog_add_mode"))
async def process_backlog_add_mode(callback: types.CallbackQuery, state: FSMContext):
    """Включает состояние FSM и ждет строку от архитектора."""
    print("\n📝 [БЭКЛОГ ЭКШЕН]: Перевод сессии пользователя в режим добавления задачи (FSM)...")
    await callback.answer()
    
    # Сначала жестко выставляем стейт
    await state.set_state(BacklogStates.waiting_for_task_text)
    # И сразу же сохраняем ID сообщения нашей текущей интерактивной шторки-меню
    await state.update_data(menu_msg_id=callback.message.message_id)
    
    print(f"🤖 [FSM]: Статус сессии изменен на: {await state.get_state()}")
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Отмена", callback_data=MenuAction(action="backlog_main").pack()))
    
    await callback.message.edit_text(
        "📝 **Режим добавления новой задачи:**\n\n"
        "Отправьте в чат текст задачи в формате:\n"
        "`<Название> | <Детальное описание> | <Приоритет 1, 2 или 3>`\n\n"
        "_Пример:_\n"
        "`Загрузка трейдов | Интеграция getNotifyTradeJson для средних цен | 3`",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@router.message(BacklogStates.waiting_for_task_text)
async def process_task_text_ingest(message: types.Message, state: FSMContext, bot: Bot):
    """Ловит текстовый месседж, парсит по черте | и шлет чистый INSERT в базу."""
    raw_text = message.text
    print(f"\n📩 [БЭКЛОГ ИНЖЕКЦИЯ]: Поймано текстовое сообщение для бэклога: '{raw_text}'")
    
    # 1. Извлекаем из памяти FSM сохраненный ID нашей интерактивной шторки
    user_data = await state.get_data()
    menu_msg_id = user_data.get("menu_msg_id")
    
    try:
        print("🧹 [БЭКЛОГ]: Удаляю исходное текстовое сообщение из чата для чистоты интерфейса...")
        await message.delete()
    except Exception as del_err:
        print(f"⚠️ [БЭКЛОГ WARNING]: Не удалось удалить сообщение пользователя: {del_err}")

    title = raw_text.strip()
    desc = "Добавлено с телефона"
    priority = 2
    
    if "|" in raw_text:
        print("✂️ [БЭКЛОГ]: Запускаю парсинг строки по разделителю '|'...")
        parts = raw_text.split("|")
        
        # Индексы на месте, очищаем пробелы строго у строк внутри списка!
        title = parts[0].strip() if len(parts) > 0 else raw_text.strip()
        if len(parts) > 1:
            desc = parts[1].strip()
        if len(parts) > 2:
            try:
                priority = int(parts[2].strip())
                if priority not in (1, 2, 3): priority = 2
            except ValueError:
                print("⚠️ [БЭКЛОГ]: Неверный формат приоритета. Выставлен дефолт: 2")
                priority = 2
                
    print(f"📋 [БЭКЛОГ ИТОГ ПАРСИНГА]:\n   ├ Название: '{title}'\n   ├ Суть: '{desc}'\n   └ Приоритет: {priority}")

    clean_title = title.replace("'", "''")
    clean_desc = desc.replace("'", "''")

    sql_insert = f"INSERT INTO public.backlog (title, description, priority, status) VALUES ('{clean_title}', '{clean_desc}', {priority}, 'todo');"
    print(f"🗄️ [БЭКЛОГ]: Отправляю инсерт в СУБД: {sql_insert}")
    
    try:
        await asyncio.to_thread(db_bot.execute_query, sql_insert)
        print("✅ [БЭКЛОГ]: Задача успешно зафиксирована в таблице public.backlog.")
    except Exception as ins_err:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА ИНСЕРТА]: Не удалось записать задачу: {ins_err}")
        
    print("🤖 [FSM]: Сбрасываю состояние ожидания текста...")
    await state.clear()
    
    # 2. Бесшовно возвращаем пользователя на обновленный экран бэклога в рамках той же шторки!
    if menu_msg_id:
        # Искусственно воссоздаем объект сообщения-меню для рендеринга
        menu_message = types.Message(
            message_id=menu_msg_id,
            date=message.date,
            chat=message.chat,
            from_user=message.from_user
        )
        # Подменяем метод edit_text, чтобы он жестко ссылался на инстанс бота
        menu_message._bot = bot
        
        print("♻️ [БЭКЛОГ]: Мгновенно обновляю старую шторку бэклога новой задачей...")
        await render_backlog_screen(menu_message, state)
    else:
        # Страховочный вариант на случай, если ID почему-то потерялся (создаст новое сообщение)
        print("⚠️ [БЭКЛОГ]: menu_msg_id не найден в FSM. Вынужден создать новое сообщение.")
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🎯 Открыть Бэклог", callback_data=MenuAction(action="backlog_main").pack()))
        await message.answer("✅ **Задача успешно зафиксирована в СУБД UPort!**", parse_mode="Markdown", reply_markup=builder.as_markup())
