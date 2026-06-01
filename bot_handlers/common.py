from aiogram.filters.callback_data import CallbackData

# Глобальная фабрика колбэков UPort, изолированная от циклического импорта
class MenuAction(CallbackData, prefix="uport"):
    action: str              # Основной экшен навигации
    portfolio_id: int = 0    # ID конкретного портфеля (0 - Сводный)
    listing_id: int = 0      # 🔥 ДОБАВЛЯЕМ ЭТУ СТРОКУ (Легализация ID листинга!)
    ticker_name: str = ""    # Имя тикера для карточки актива
    sub_view: str = ""       # Под-вкладка для Экранов 2 и 3
    task_id: int = 0         # ID задачи бэклога для кнопок закрытия
