# Добавить:
# Мы очищаем базу не по капризному флагу deleted от брокера, а по факту неактивности и древности записи [单元].На уровне SQL-команды хрон ночью выполняет ровно один зрячий запрос:sqlDELETE FROM public.alerts 
# WHERE is_active = false 
#   AND updated_at < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - INTERVAL '1 day';

import asyncio
import logging
from datetime import datetime, timezone

# Напрямую импортируем наши изолированные рабочие утилиты
from site_connectors.sync_rates_yhoo import sync_rates
from site_connectors.sync_fundamentals_yhoo import sync_fundamentals
from brokers_connectors.sync_quotes_fb import sync_quotes_fb_autonomous
from brokers_connectors.sync_quotes_t212 import sync_quotes_t212_branch
from brokers_connectors.sync_alerts_fb import sync_all_broker_alerts
from site_connectors.trigger_etf_look_through import run_etf_look_through_decomposition
from site_connectors.sync_signals_yf import sync_global_yahoo_signals
from utils import was_us_market_open_yesterday
from analytics.daily_digest import assemble_portfolio_digest_data
from analytics.portfolio_inspector import PortfolioInspector
from analytics.volatility_utils import compute_portfolio_nav_volatility
from bot_handlers.bot_screens import render_digest_overview_text
from bot_handlers.bot_keyboards import generate_digest_toc_keyboard



# ─────────────────────────────────────────────────────────
# === ГЛОБАЛЬНЫЕ НАСТРОЙКИ ПЕРИОДИЧНОСТИ (В СЕКУНДАХ) ===
# ─────────────────────────────────────────────────────────
QUOTES_SYNC_INTERVAL = 900          # 15 минут для котировок акций
RATES_SYNC_INTERVAL = 7200          # 2 часа для макрокурсов валют Форекс
SIGNALS_CHECK_INTERVAL = 3600       # 1 час для проверки ночного реле рыночных сигналов
FUNDAMENTALS_CHECK_INTERVAL = 3600  # 1 час для проверки ночного реле фундаментала
ETF_LT_CHECK_INTERVAL = 3600        # 1 час для проверки воскресного будильника ETF
ALERTS_SYNC_INTERVAL = 300          # 5 минут для планового сбора алертов Freedom Broker
JANITOR_CHECK_INTERVAL = 3600       # 1 час для проверки будильника дворника СУБД
DIGEST_CHECK_INTERVAL = 3600        # 1 час для проверки будильника утреннего дайджеста

# 🔥 СТАНДАРТ v3.0: ДИНАМИЧЕСКИЙ ТАЙМЛАЙН СИНХРОНИЗАЦИИ С РЫНКОМ США (UTC)
# Биржи NYSE/NASDAQ закрываются в 16:00 по Нью-Йорку (летом это 20:00 UTC, зимой - 21:00 UTC).
# Задаем константу 21 с часом запаса. В Москве на часах в этот момент будет ровно 00:00 (полночь).
USA_MARKET_CLOSE_HOUR_UTC = 21

# Переводим целевые часы из абсолютных в ОТНОСИТЕЛЬНЫЕ с защитным оператором % 24
FUNDAMENTALS_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 3) % 24        
JANITOR_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 5) % 24 
ETF_LT_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 9) % 24
SIGNALS_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 2) % 24
DIGEST_TARGET_HOUR = (USA_MARKET_CLOSE_HOUR_UTC + 8) % 24  # 21+8=29%24=5 -> 05:00 UTC = 08:00 Москва


# === НАПРЯМЫЕ ФУНКЦИИ ОБНОВЛЕНИЯ И ОЧИСТКИ ===

def run_quotes_update(db_instance):
    """Сбор тикеров из базы и точечный вызов REST-коннекторов брокеров с фильтром tracking_status."""
    logging.info("⚡ Прямой запуск обновления котировок акций...")
    
    all_brokers = db_instance.execute_query("SELECT id, name FROM public.brokers;")
    if not all_brokers:
        logging.warning("[Cron]: Справочник brokers пуст.")
        return

    for broker in all_brokers:
        b_id = int(broker['id'])
        b_name = broker['name']
        
        # Обработка Freedom Broker (ID: 1) на новых автономных рельсах
        if b_id == 1:
            logging.info(f"💼 [Cron]: Запуск автономного обновления котировок для брокера {b_name} (ID: 1)")
            sync_quotes_fb_autonomous()
            
        # Обработка Trading 212 (ID: 2) по старой схеме до ее модернизации
        elif b_id == 2:
            sql_tickers = """
                SELECT DISTINCT ON (l.broker_symbol) l.id AS listing_id, l.broker_symbol AS full_ticker, c.multiplier
                FROM public.watchlist w
                JOIN public.listings l ON w.listing_id = l.id
                JOIN public.portfolios p ON w.portfolio_id = p.id
                JOIN public.currencies c ON l.currency_id = c.id
                WHERE p.broker_id = %s AND (w.watched_at IS NOT NULL OR w.ordered_at IS NOT NULL OR w.bought_at IS NOT NULL);
            """
            tickers_data = db_instance.execute_query(sql_tickers, (b_id,))
            if not tickers_data:
                continue
                
            logging.info(f"💼 [Cron]: Обновляю {len(tickers_data)} активных листингов у брокера {b_name} (ID: {b_id})")
            sync_quotes_t212_branch(tickers_data, db_instance)

def run_rates_update(db_instance):
    """Прямой вызов сбора Форекс-курсов через Yahoo Finance."""
    logging.info("⚡ Прямой запуск обновления курсов валют...")
    sync_rates(db_instance)

def run_database_janitor(db_instance):
    """🔥 ДВОРНИК СУБД: Выполняет плановое удаление протухшего мусора из таблиц по ТЗ."""
    logging.info("🧹 [ДВОРНИК]: Старт суточной очистки и стерилизации базы данных UPort...")
    
    # 🔥 ПРАВКА №1: Очистка неактивных алертов по факту древности (> 1 дня) строго по UTC
    sql_clean_alerts = """
        DELETE FROM public.alerts 
        WHERE is_active = false 
          AND updated_at < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - INTERVAL '1 day';
    """
    
    # 🔥 ПРАВКА №2 (пересмотрено 2026-08-17, Claude/BACKLOG.md №123): раньше держали
    # проданную позицию в СН 30 дней -- "последить месяц, вдруг захочу перезайти".
    # Сценарий устранён (перезаход теперь идёт через обычные побуждения, не через
    # залежавшуюся строку) -- строка без возрастного порога никому больше не служит:
    # мешает digest'у честно предложить "добавить" при новой рекомендации на ту же
    # бумагу. Чистим безусловно на каждом ночном прогоне.
    sql_clean_watchlist = """
        DELETE FROM public.watchlist
        WHERE sold_out_at IS NOT NULL;
    """
    
    try:
        db_instance.execute_query(sql_clean_alerts)
        db_instance.execute_query(sql_clean_watchlist)
        logging.info("🧹 [ДВОРНИК УСПЕХ]: Стерилизация завершена. Протухшие алерты (>1д) и закрытые вотчлисты (>30д) стёрты.")
    except Exception as janitor_err:
        logging.error(f"❌ [ДВОРНИК КРИТИЧЕСКИЙ СБОЙ]: Ошибка очистки СУБД: {janitor_err}")

async def send_daily_digests(db_instance, bot):
    """
    Дайджест по каждому портфелю уходит реальному владельцу портфеля (если у
    владельца уже включена доставка -- пока явно только owner_id 1/2, см.
    Claude/BACKLOG.md, сын ещё не готов) и дублируется админу. Если владелец
    портфеля сам админ (owner_id=1) -- дедуп по chat_id, сообщение уходит
    только один раз, не дважды в один чат.
    """
    OWNERS_WITH_DIGEST_DELIVERY = (1, 2)  # временный явный список -- расширить, когда сын онбордингнут

    admin_row = await asyncio.to_thread(
        db_instance.execute_row,
        "SELECT telegram_id FROM public.users WHERE is_admin = true LIMIT 1;"
    )
    admin_telegram_id = (admin_row or {}).get("telegram_id")
    if not admin_telegram_id:
        logging.error("❌ [Digest]: Не найден telegram_id администратора -- дайджест некому отправить.")
        return {"processed": 0, "errors": 1}

    portfolios = await asyncio.to_thread(
        db_instance.execute_query,
        "SELECT id, name, owner_id FROM public.portfolios ORDER BY id;"
    )
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    # Честная статистика (Claude/BACKLOG.md №28) -- раньше run_daily_job_once видел только
    # "долетело ли исключение до самой обёртки", а этот цикл проглатывал сбой по ОТДЕЛЬНОМУ
    # портфелю через try/except внутри и всегда возвращал None (=SUCCESS), даже если часть
    # дайджестов не собралась/не отправилась.
    error_count = 0

    for p in portfolios:
        p_id = int(p["id"])
        owner_id = p.get("owner_id")

        recipients = {admin_telegram_id}
        if owner_id in OWNERS_WITH_DIGEST_DELIVERY:
            owner_row = await asyncio.to_thread(
                db_instance.execute_row,
                "SELECT telegram_id FROM public.users WHERE id = %s;",
                (owner_id,)
            )
            owner_telegram_id = (owner_row or {}).get("telegram_id")
            if owner_telegram_id:
                recipients.add(owner_telegram_id)

        try:
            data = await asyncio.to_thread(assemble_portfolio_digest_data, db_instance, p_id)
            text = render_digest_overview_text(data)
            keyboard = generate_digest_toc_keyboard(p_id, data["sections"])
            for chat_id in recipients:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
            logging.info(f"✅ [Digest]: Дайджест по портфелю {p['name']} (ID: {p_id}) отправлен {len(recipients)} получателям.")
        except Exception as e:
            error_count += 1
            logging.error(f"❌ [Digest]: Не удалось собрать/отправить дайджест по портфелю {p['name']} (ID: {p_id}): {e}")

    return {"processed": len(portfolios), "errors": error_count}


async def snapshot_portfolio_values(db_instance):
    """
    Фаза 3 темы «Бумажный портфель» (Claude/14_paper_portfolio.md) -- суточный снимок
    NAV в portfolio_value_history, нужен для измерения доходности через год (сравнение
    с S&P500 и с целевыми ~15% годовых), и как источник истории для дневной волатильности
    NAV (Сигнал D, Claude/23_session_followups_2026-08-20.md). Функция и таблица сделаны
    портфель-агностично (переиспользуемый механизм, не одноразовый хак под «ПБум») --
    раньше фильтровала только CONFIRM-портфели ("ПБум"), с 2026-08-21 снято: Сигналу D
    нужна история NAV для ВСЕХ портфелей, не только бумажного.
    """
    portfolios = await asyncio.to_thread(
        db_instance.execute_query,
        "SELECT id, name FROM public.portfolios;"
    )
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    error_count = 0
    for p in portfolios:
        p_id = int(p["id"])
        try:
            inspector = PortfolioInspector(db_instance, p_id)
            total_value = await asyncio.to_thread(inspector.calculate_total_capital)
            # SELECT-затем-UPDATE/INSERT, не ON CONFLICT (Claude/BACKLOG.md №128) -- гонки
            # нет: единственный писатель в эту таблицу, раз в сутки, последовательный цикл.
            existing_row = await asyncio.to_thread(
                db_instance.execute_row,
                "SELECT id FROM public.portfolio_value_history WHERE portfolio_id = %s AND snapshot_date = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date;",
                (p_id,)
            )
            if existing_row:
                await asyncio.to_thread(
                    db_instance.execute_query,
                    "UPDATE public.portfolio_value_history SET total_value = %s WHERE id = %s;",
                    (float(total_value), int(existing_row["id"]))
                )
            else:
                await asyncio.to_thread(
                    db_instance.execute_query,
                    """
                        INSERT INTO public.portfolio_value_history (portfolio_id, snapshot_date, total_value)
                        VALUES (%s, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date, %s);
                    """,
                    (p_id, float(total_value))
                )
            logging.info(f"📸 [NAV Snapshot]: '{p['name']}' (ID: {p_id}) -- ${float(total_value):,.2f}")

            nav_volatility = await asyncio.to_thread(compute_portfolio_nav_volatility, db_instance, p_id)
            if nav_volatility is not None:
                await asyncio.to_thread(
                    db_instance.execute_query,
                    "UPDATE public.portfolios SET nav_daily_volatility_pct = %s WHERE id = %s;",
                    (nav_volatility, p_id)
                )
        except Exception as e:
            error_count += 1
            logging.error(f"❌ [NAV Snapshot]: Не удалось снять снимок для '{p['name']}' (ID: {p_id}): {e}")

    return {"processed": len(portfolios), "errors": error_count}

# === ПЕРСИСТЕНТНАЯ ЗАЩИТА "УМНЫХ БУДИЛЬНИКОВ" ОТ ПОВТОРНОГО СРАБАТЫВАНИЯ ===

async def run_daily_job_once(db_instance, job_name: str, coro_factory):
    """
    Универсальный враппер для суточных/недельных будильников.
    Проверяет через public.cron_job_runs (не через переменную в памяти -- она не
    переживает рестарт процесса, а uport.service настроен на Restart=always и
    реально перезапускается по нескольку раз в день), не выполнялась ли уже эта
    задача сегодня (по UTC-дате), и честно фиксирует SUCCESS/FAILED по факту
    исключения (а не безусловное "успех" в логе).

    coro_factory -- функция без аргументов, возвращающая awaitable (например,
    lambda: asyncio.to_thread(sync_fundamentals, db_instance)).
    """
    today_utc = datetime.now(timezone.utc).date()

    row = await asyncio.to_thread(
        db_instance.execute_row,
        "SELECT last_started_at FROM public.cron_job_runs WHERE job_name = %s;",
        (job_name,)
    )
    last_started_raw = (row or {}).get("last_started_at")
    if last_started_raw:
        # СУБД-шлюз иногда отдаёт timestamp строкой, иногда готовым datetime -- приводим явно
        last_started_dt = (
            datetime.fromisoformat(last_started_raw) if isinstance(last_started_raw, str) else last_started_raw
        )
        if last_started_dt.date() == today_utc:
            logging.info(f"⏭️ [Cron]: '{job_name}' уже выполнялась сегодня ({today_utc}), пропускаю повтор.")
            return

    await asyncio.to_thread(db_instance.execute_query, """
        INSERT INTO public.cron_job_runs (job_name, last_started_at, last_status, last_error_message)
        VALUES (%s, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0), 'RUNNING', NULL)
        ON CONFLICT (job_name) DO UPDATE SET
            last_started_at = EXCLUDED.last_started_at,
            last_status = 'RUNNING',
            last_finished_at = NULL,
            last_error_message = NULL;
    """, (job_name,))

    try:
        result = await coro_factory()
    except Exception as e:
        error_text = str(e)[:2000]
        await asyncio.to_thread(db_instance.execute_query, """
            UPDATE public.cron_job_runs
            SET last_finished_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                last_status = 'FAILED',
                last_error_message = %s
            WHERE job_name = %s;
        """, (error_text, job_name))
        raise
    else:
        # Честная статистика (Claude/BACKLOG.md, п.25, 2026-08-03): раньше "упало ли всё
        # целиком" (долетело исключение) был единственным различием -- частичный сбой внутри
        # успешно завершившегося вызова (часть тикеров в батче тихо упала) не был виден.
        # coro_factory может вернуть {"processed": N, "errors": K} (переводится постепенно,
        # не все синхронизаторы ещё дают эту структуру) -- если errors>0, пишем PARTIAL,
        # иначе (в т.ч. старое поведение "вернул None") -- SUCCESS, как раньше.
        status = "SUCCESS"
        error_note = None
        if isinstance(result, dict) and int(result.get("errors") or 0) > 0:
            status = "PARTIAL"
            error_note = f"{result['errors']} из {result.get('processed', '?')} не обработалось"

        await asyncio.to_thread(db_instance.execute_query, """
            UPDATE public.cron_job_runs
            SET last_finished_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                last_status = %s,
                last_error_message = %s
            WHERE job_name = %s;
        """, (status, error_note, job_name))


# === ПЕТЛИ ВРЕМЕНИ (РЕЛЕ БОЕВЫХ ЦИКЛОВ) ===

async def alerts_clock_loop(db_instance):
    """🔥 РЕЛЕ АЛЕРТОВ ФБ: Непрерывный опрос брокера раз в 5 минут."""
    logging.info(f"⏱️ [Реле Времени]: Контур планового сбора алертов Freedom Broker взведен (Раз в {ALERTS_SYNC_INTERVAL}с).")
    while True:
        try:
            # Вызываем наш боевой синхронизатор в изолированном асинхронном потоке thread
            await asyncio.to_thread(sync_all_broker_alerts)
        except Exception as e:
            logging.error(f"❌ [Cron Алертов Error]: {e}")
        await asyncio.sleep(ALERTS_SYNC_INTERVAL)

async def janitor_clock_loop(db_instance):
    """🔥 УМНЫЙ СУТОЧНЫЙ БУДИЛЬНИК ДВОРНИКА: Просыпается раз в час и ждет 5:00 утра."""
    logging.info(f"⏱️ [Реле Времени]: Контур суточного Дворника СУБД взведен на дежурство (Цель: {JANITOR_TARGET_HOUR:02d}:00 утра).")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == JANITOR_TARGET_HOUR:
                logging.info(f"🧹 [Cron]: На часах {JANITOR_TARGET_HOUR:02d}:00 утра. Запуск планового Дворника...")
                await run_daily_job_once(
                    db_instance, "db_janitor",
                    lambda: asyncio.to_thread(run_database_janitor, db_instance)
                )
        except Exception as e:
            logging.error(f"❌ [Janitor Clock Error]: {e}")
            
        await asyncio.sleep(JANITOR_CHECK_INTERVAL)

async def quotes_clock_loop(db_instance):
    """Реле времени цен акций (раз в 15 минут)"""
    logging.info("⏱️ [Реле Времени]: Контур цен акций взведен.")
    while True:
        try:
            await asyncio.to_thread(run_quotes_update, db_instance)
        except Exception as e:
            logging.error(f"❌ [Cron Цен Error]: {e}")
        await asyncio.sleep(QUOTES_SYNC_INTERVAL)

async def rates_clock_loop(db_instance):
    """Реле времени макрокурсов валют (раз в 2 часа)"""
    logging.info("⏱️ [Реле Времени]: Контур курсов валют успешно взведен.")
    while True:
        try:
            await asyncio.to_thread(run_rates_update, db_instance)
        except Exception as e:
            logging.error(f"❌ [Cron Валют Error]: {e}")
        await asyncio.sleep(RATES_SYNC_INTERVAL)

async def etf_lt_weekly_clock_loop(db_instance):
    """🔥 УМНЫЙ ВОСКРЕСНЫЙ БУДИЛЬНИК ETF: Просыпается раз в час, ждет воскресенья и целевого часа."""
    logging.info(f"⏱️ [Реле Времени]: Контур воскресного Look-Through ETF взведен (Цель: Воскресенье, {ETF_LT_TARGET_HOUR:02d}:00 утра UTC).")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            current_day = now_utc.weekday()  # 6 — это воскресенье в Python
            current_hour = now_utc.hour

            if current_day == 6 and current_hour == ETF_LT_TARGET_HOUR:
                logging.info(f"🧱 [Cron]: Воскресенье, на часах {ETF_LT_TARGET_HOUR:02d}:00 утра UTC. Запуск плановой массовой декомпозиции фондов...")
                # Вызываем асинхронную функцию напрямую без параметров для массового обхода Universe
                await run_daily_job_once(
                    db_instance, "etf_look_through_weekly",
                    lambda: run_etf_look_through_decomposition(target_ticker_id=None)
                )
                logging.info("✅ [Cron]: Воскресный профилактический обход ETF успешно завершен.")
        except Exception as e:
            logging.error(f"❌ [ETF LT Weekly Clock Error]: {e}")

        await asyncio.sleep(ETF_LT_CHECK_INTERVAL)

async def fundamentals_clock_loop(db_instance):
    """УМНОЕ НОЧНОЕ РЕЛЕ ВРЕМЕНИ: Просыпается раз в час и ждет 3:00 ночи."""
    logging.info("⏱️ [Реле Времени]: Контур фундаментального анализа акций УОЛЛ-СТРИТ взведен.")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == FUNDAMENTALS_TARGET_HOUR:
                logging.info(f"🌙 [Cron]: На часах {FUNDAMENTALS_TARGET_HOUR:02d}:00 ночи. Запуск планового анализа...")
                await run_daily_job_once(
                    db_instance, "fundamentals_sync",
                    lambda: asyncio.to_thread(sync_fundamentals, db_instance)
                )
        except Exception as e:
            logging.error(f"❌ [Cron Фундаментал Error]: {e}")
            
        await asyncio.sleep(FUNDAMENTALS_CHECK_INTERVAL)

async def signals_clock_loop(db_instance):
    """🔥 УМНОЕ НОЧНОЕ РЕЛЕ СИГНАЛОВ: Просыпается раз в час и ждет 23:00 UTC для расчета теханализа Yahoo."""
    logging.info(f"⏱️ [Реле Времени]: Контур рыночных сигналов Yahoo взведен на дежурство (Цель: {SIGNALS_TARGET_HOUR:02d}:00 вечера UTC).")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == SIGNALS_TARGET_HOUR:
                logging.info(f"🌅 [Cron]: На часах {SIGNALS_TARGET_HOUR:02d}:00 вечера UTC. Запуск пакетного конвейера сигналов...")
                # Запускаем тяжелый синхронизатор в изолированном потоке, чтобы не блокировать асинхронный цикл
                await run_daily_job_once(
                    db_instance, "signals_sync",
                    lambda: asyncio.to_thread(sync_global_yahoo_signals, single_ticker_id=None)
                )
                logging.info("✅ [Cron]: Глобальный технический скоринг Yahoo успешно завершен.")
        except Exception as e:
            logging.error(f"❌ [Cron / Настройки Сигналов Error]: {e}")
            
        await asyncio.sleep(SIGNALS_CHECK_INTERVAL)

async def digest_clock_loop(db_instance, bot):
    """🔥 УМНЫЙ УТРЕННИЙ БУДИЛЬНИК ДАЙДЖЕСТА: Просыпается раз в час и ждёт целевого часа."""
    logging.info(f"⏱️ [Реле Времени]: Контур утреннего дайджеста взведен на дежурство (Цель: {DIGEST_TARGET_HOUR:02d}:00 UTC).")
    while True:
        try:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == DIGEST_TARGET_HOUR:
                if not await asyncio.to_thread(was_us_market_open_yesterday):
                    logging.info("⏭️ [Digest]: Вчера рынок США не торговал -- пропускаю дайджест (нечего нового сообщить).")
                else:
                    logging.info(f"🌅 [Cron]: На часах {DIGEST_TARGET_HOUR:02d}:00 UTC. Сборка утреннего дайджеста...")
                    await run_daily_job_once(
                        db_instance, "daily_digest",
                        lambda: send_daily_digests(db_instance, bot)
                    )
                    # BUY/SELL/TRIM_DOWN бумажного портфеля переехали на кнопку исполнения
                    # прямо в дайджесте (Claude/BACKLOG.md №122/123, 2026-08-17/18) --
                    # отдельные трубы (send_paper_buy/sell/trim_recommendations) больше не нужны.
                    # Фаза 3 -- суточный снимок NAV для измерения доходности.
                    await run_daily_job_once(
                        db_instance, "portfolio_value_snapshot",
                        lambda: snapshot_portfolio_values(db_instance)
                    )
        except Exception as e:
            logging.error(f"❌ [Digest Clock Error]: {e}")

        await asyncio.sleep(DIGEST_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────
# === ГЛАВНАЯ ТОЧКА СБОРКИ ВСЕХ ПЕТЕЛЬ ВРЕМЕНИ CRON ===
# ─────────────────────────────────────────────────────────
async def start_clocks(db_instance, bot):
    """Оркестратор планировщика UPort: собирает все циклы в единую асинхронную группу."""
    logging.info("🚀 [Планировщик UPort]: Запуск всех контуров реле времени фоновых задач...")

    # Первичный принудительный синхрон котировок, курсов и алертов при старте ОС сервера
    try:
        await asyncio.to_thread(run_quotes_update, db_instance)
        await asyncio.to_thread(run_rates_update, db_instance)
        await asyncio.to_thread(sync_all_broker_alerts)  # Первичный сбор алертов при старте системы
    except Exception as boot_err:
        logging.error(f"⚠️ Ошибка первичной инициализации данных при загрузке Cron: {boot_err}")

    # Запускаем бесконечные асинхронные петли в Event Loop
    await asyncio.gather(
        quotes_clock_loop(db_instance),
        rates_clock_loop(db_instance),
        fundamentals_clock_loop(db_instance),
        signals_clock_loop(db_instance),       # Новое ночное реле рыночных сигналов на 23:00 UTC
        alerts_clock_loop(db_instance),       # 5-минутный контур алертов Freedom Broker
        janitor_clock_loop(db_instance),      # Суточный контур Дворника СУБД на 02:00 ночи UTC
        etf_lt_weekly_clock_loop(db_instance),  # Новое воскресное реле декомпозиции ETF на 06:00 утра UTC
        digest_clock_loop(db_instance, bot)    # Утренний дайджест на 05:00 UTC (08:00 Москва)
    )