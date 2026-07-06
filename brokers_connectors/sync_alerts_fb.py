import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Настройка путей для корректного поиска модулей экосистемы UPort
sys.path.append(str(Path(__file__).parent.parent))

from database import db_sys
from brokers_connectors.fb_client import FreedomBrokerClient

# Настройка логирования для крона
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def sync_all_broker_alerts():
    logging.info("📡 [UPort ALERTS WORKER]: Старт контура синхронизации алертов Freedom Broker...")
    
    # 1. Выгребаем только те портфели, которые работают через Freedom Broker (broker_id = 1)
    # Прямо из реляции узнаем user_id (owner_id) для заполнения поля created_by_user_id
    portfolios_sql = """
        SELECT id AS portfolio_id, name AS portfolio_name, owner_id AS user_id 
        FROM public.portfolios 
        WHERE broker_id = 1;
    """
    try:
        portfolios_list = db_sys.execute_query(portfolios_sql)
    except Exception as db_err:
        logging.error(f"❌ Критический сбой при запросе портфелей из БД: {db_err}")
        return

    if not portfolios_list:
        logging.info("💡 Активных портфелей Freedom Broker в базе UPort не найдено.")
        return

    # 2. Бежим циклом по каждому портфелю Freedom Broker отдельно
    for port in portfolios_list:
        portfolio_id = port['portfolio_id']
        portfolio_name = port['portfolio_name']
        user_id = port['user_id']
        
        if not user_id:
            logging.warning(f"⚠️ У портфеля '{portfolio_name}' (ID: {portfolio_id}) не указан владелец. Пропуск.")
            continue
            
        logging.info(f"💼 Обработка портфеля: '{portfolio_name}' (ID: {portfolio_id}), Владелец ID: {user_id}")
        
        # Вызываем нашу новую фабрику. Она сама сходит в базу за префиксом и заберет ключи из .env
        fb_client = FreedomBrokerClient.create_for_user(user_id=user_id, db_instance=db_sys)
        if not fb_client:
            logging.warning(f"⏩ Пропускаю синхронизацию портфеля '{portfolio_name}', так как клиент не собран.")
            continue
            
        # Запрашиваем у брокера массив алертов для данного пользователя
        broker_alerts = fb_client.get_active_alerts()
        
        # Заводим изолированный список для сбора живых ID алертов конкретно этого портфеля
        synced_alert_ids = []
        
        if not broker_alerts:
            logging.info(f"🕊️ На стороне брокера нет алертов для портфеля '{portfolio_name}'.")
        else:
            logging.info(f"📥 Получено {len(broker_alerts)} алертов от брокера. Начинаю построчный разбор...")
            
            # Построчный разбор массива алертов
            for b_al in broker_alerts:
                b_alert_id = b_al.get('id')
                b_ticker = b_al.get('ticker')
                
                if not b_alert_id or not b_ticker:
                    continue
                    
                b_alert_id = int(b_alert_id)
                b_ticker = str(b_ticker).upper().strip()

                # ─── ВРЕМЕННЫЙ ДИАГНОСТИЧЕСКИЙ ПЕРЕХВАТ СЫРЫХ ДАННЫХ БРОКЕРА ───
                if b_ticker == "RKLB.US":
                    logging.info(
                        f"📡 [RAW FB ALERT DATA] Найдена RKLB! "
                        f"broker_alert_id: {b_alert_id} | "
                        f"Сырое поле deleted: 【 {b_al.get('deleted')} 】 | "
                        f"Сырое поле triggered: 【 {b_al.get('triggered')} 】"
                    )
                
                # 🛑 ФИЛЬТР УДАЛЕННЫХ (Каноническое правило из официальной документации ФБ)
                if str(b_al.get('deleted')).strip() == '1':
                    continue

                # 🔥 ШАГ 2: БОРТОВОЙ ЗАГРАДИТЕЛЬНЫЙ ЩИТ ПРОТИВ МЕРТВЫХ (АРХИВНЫХ) АЛЕРТОВ
                # Согласно манифесту API, если поле triggered содержит дату — алерт отработал в прошлом
                b_triggered_raw = b_al.get('triggered')
                is_already_triggered = b_triggered_raw and str(b_triggered_raw).strip() != ""
                
                if is_already_triggered:
                    # Если алерт на бирже уже мертв, зряче проверяем — заведен ли он вообще в нашей СУБД?
                    sql_check_zombie = f"SELECT id FROM public.alerts WHERE portfolio_id = {int(portfolio_id)} AND broker_alert_id = {b_alert_id} LIMIT 1;"
                    zombie_rows = db_sys.execute_query(sql_check_zombie)
                    
                    if not zombie_rows:
                        # СЦЕНАРИЙ А: Вы вычистили его из базы вручную (или ночью стёр хрон), а у брокера он в архиве.
                        # Намертво блокируем повторное создание архивных призраков в СУБД, спасая ID!
                        continue
                
                # Добавляем ID в наш список гарантированно живых на бирже алертов
                synced_alert_ids.append(b_alert_id)

                # 3. ЛЕГАЛИЗАЦИЯ И СВЯЗЫВАНИЕ ТИКЕРА
                # Передаем готовый fb_client текущего пользователя для возможных проверок
                al_ticker_id, al_listing_id = db_sys.ensure_ticker_v3(
                    ticker_name_raw=b_ticker,
                    caller_role="LST",
                    caller_id=None,
                    broker_id=1,
                    fb_client=fb_client
                )

                # Если тикер абсолютно неизвестен глобальной системе — безопасно пропускаем
                if not al_ticker_id:
                    logging.warning(f"⚠️ Тикер {b_ticker} отсутствует в системе UPort. Алерт #{b_alert_id} пропущен.")
                    continue

                # Если листинг для Freedom Broker (broker_id = 1) еще не заведен — создаем его честно
                if not al_listing_id:
                    logging.info(f"🔍 Листинг для {b_ticker} у брокера FB не найден. Запрашиваю спецификацию...")
                    
                    # Отправляем запрос к API за реальной спецификацией бумаги
                    sec_info = fb_client.get_security_info(b_ticker)
                    b_currency = sec_info.get('currency') if isinstance(sec_info, dict) else None
                    
                    if b_currency:
                        b_currency = str(b_currency).upper().strip()
                    else:
                        b_currency = None  # Строго NULL, если брокер не отдал валюту (никаких костылей)

                    # Заносим новую бумагу в таблицу листингов брокера
                    insert_listing_sql = f"""
                        INSERT INTO public.listings (ticker_id, broker_id, broker_symbol, currency_id, last_price)
                        VALUES ({int(al_ticker_id)}, 1, '{b_ticker}', {f"'{b_currency}'" if b_currency else 'NULL'}, 0)
                        RETURNING id;
                    """
                    try:
                        new_listing_rows = db_sys.execute_query(insert_listing_sql)
                        if new_listing_rows:
                            # Извлекаем сгенерированный базой id нового листинга
                            al_listing_id = int(new_listing_rows[0]['id'])
                            logging.info(f"✅ Создан новый листинг ID: {al_listing_id} для {b_ticker} ({b_currency})")
                    except Exception as l_err:
                        logging.error(f"❌ Не удалось создать листинг для {b_ticker}: {l_err}")
                        continue

                # Если листинг теперь гарантированно есть — прописываем его в фокус стратегии портфеля
                if al_listing_id:
                    db_sys.ensure_watchlist_row_v2(portfolio_id=portfolio_id, listing_id=al_listing_id, reason="watched")

                # 4. ГЛУБОКИЙ ПАРСИНГ ПАРАМЕТРОВ АЛЕРТА
                init_price = float(b_al.get('init_price') or 0)
                raw_trig_price = b_al.get('trigger_price', '{}')
                
                # Распаковываем JSON-строку цен во внутренний словарь
                trig_data = json.loads(raw_trig_price) if isinstance(raw_trig_price, str) else raw_trig_price
                if not isinstance(trig_data, dict):
                    trig_data = {}

                trigger_price = 0.0
                trigger_price_min = None
                trigger_price_max = None
                trigger_pct = None

                if 'price' in trig_data:
                    trigger_price = float(trig_data.get('price') or 0)
                elif 'min_price' in trig_data or 'max_price' in trig_data:
                    trigger_price_min = float(trig_data.get('min_price')) if trig_data.get('min_price') else None
                    trigger_price_max = float(trig_data.get('max_price')) if trig_data.get('max_price') else None
                elif 'pct' in trig_data:
                    trigger_pct = float(trig_data.get('pct') or 0)

                # ВЫЧИСЛЕНИЕ МАТЕМАТИЧЕСКОГО ЗНАКА УСЛОВИЯ (< или >)
                t_type = b_al.get('trigger_type', 'crossing')
                if t_type in ('crossing_down', 'less_then'):
                    condition = '<'
                elif t_type in ('crossing_up', 'greater_then'):
                    condition = '>'
                else:
                    condition = '<' if (trigger_price and trigger_price < init_price) else '>'

                # СНАЙПЕРСКИЙ РАЗБОР СРОКОВ ДЕЙСТВИЯ (UTC СТАНДАРТ)
                # ─── ТОЧЕЧНАЯ ПРАВКА №1: СТЕРИЛИЗАЦИЯ ДАТЫ ДО СЕКУНД UTC БЕЗ ТАЙМЗОН ───
                expire_raw = str(b_al.get('expire', '0')).strip()
                
                # Если от брокера прилетела строка с миллисекундами, принудительно отсекаем их для expire_raw
                if '.' in expire_raw and not expire_raw.isdigit():
                    expire_raw = expire_raw.split('.')[0]

                expire_type = 'good_till_cancelled'
                expire_at = None

                if expire_raw == 'end_of_day':
                    expire_type = 'end_of_day'
                    # Генерируем наивное UTC-время конца дня без часовых поясов и микросекунд
                    expire_at = datetime.utcnow().replace(hour=23, minute=59, second=58, microsecond=0, tzinfo=None)
                elif expire_raw != '0' and expire_raw != '':
                    expire_type = 'good_till_date'
                    if expire_raw.isdigit():
                        try:
                            # Извлекаем наивную UTC-дату из timestamp
                            expire_at = datetime.utcfromtimestamp(int(expire_raw)).replace(microsecond=0, tzinfo=None)
                        except Exception:
                            expire_at = None
                    else:
                        try:
                            # Парсим чистую строку даты БЕЗ временных сдвигов
                            expire_at = datetime.strptime(expire_raw, "%Y-%m-%d %H:%M:%S").replace(microsecond=0, tzinfo=None)
                        except Exception:
                            expire_at = None

                # УПРАВЛЕНИЕ СТАТУСОМ АКТИВНОСТИ И СРАБАТЫВАНИЯ
                is_trig = b_al.get('triggered', '')
                periodic = int(b_al.get('periodic') or 0)
                
                # По умолчанию алерт считается активным
                uport_active = True
                triggered_at = None

                # Если брокер взвел флаг triggered, значит в данные 5 минут он сработал
                if is_trig and str(is_trig).strip():
                    triggered_at = datetime.utcnow().replace(microsecond=0, tzinfo=None)
                    # Если алерт однократный (periodic == 0), мы гасим его активность в UPort
                    if periodic == 0:
                        uport_active = False
                        logging.info(f"🔥 Однократный алерт #{b_alert_id} ({b_ticker}) отработал и закрыт.")
                    else:
                        logging.info(f"🔄 Периодический алерт #{b_alert_id} ({b_ticker}) зафиксировал срабатывание, но остается активным.")

                # ─── ШАГ 3: ЗРЯЧАЯ ДВУХЭТАПНАЯ ЗАПИСЬ В СУБД БЕЗ ON CONFLICT (СПАСЕНИЕ ID) ───
                
                # Вспомогательная функция форматирования наивных UTC-значений для SQL
                def sql_val(v):
                    if v is None: return "NULL"
                    if isinstance(v, bool): return "true" if v else "false"
                    if isinstance(v, (int, float)): return str(v)
                    if isinstance(v, datetime): return f"'{v.strftime('%Y-%m-%d %H:%M:%S')}'"
                    return f"'{str(v).replace(chr(39), chr(39)+chr(39))}'"

                # 🔎 Прицельно проверяем кэш СУБД по бизнес-ключу брокера
                sql_check_alert = f"""
                    SELECT id FROM public.alerts 
                    WHERE portfolio_id = {int(portfolio_id)} 
                      AND broker_alert_id = {b_alert_id} 
                    LIMIT 1;
                """
                existing_alert_rows = db_sys.execute_query(sql_check_alert)

                if existing_alert_rows and len(existing_alert_rows) > 0:
                    # 📝 ВЕТКА UPDATE: Алерт уже существует. Обновляем точечно по его ID. 
                    # Счётчик автоинкремента таблицы (спидометр ID) полностью спит!
                    row_id = int(existing_alert_rows[0]['id'])
                    
                    sql_update_alert = f"""
                        UPDATE public.alerts 
                        SET listing_id = {int(al_listing_id)},
                            ticker_id = {int(al_ticker_id)},
                            trigger_price = {trigger_price},
                            trigger_price_min = {sql_val(trigger_price_min)},
                            trigger_price_max = {sql_val(trigger_price_max)},
                            trigger_pct = {sql_val(trigger_pct)},
                            expire_type = {sql_val(expire_type)},
                            expire_at = {sql_val(expire_at)},
                            triggered_status = {sql_val(str(is_trig))},
                            is_active = {sql_val(uport_active)},
                            triggered_at = COALESCE(triggered_at, {sql_val(triggered_at)}),
                            updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                        WHERE id = {row_id};
                    """
                    db_sys.execute_query(sql_update_alert)
                    logging.info(f"📝 [SYNC ALERTS]: Точечно обновлен алерт #{b_alert_id} (ID строки: {row_id}, Активен: {uport_active})")
                else:
                    # ➕ ВЕТКА INSERT: Абсолютно новый живой алерт на рынке.
                    # Берем чистый, следующий по порядку ID без холостого выжигания.
                    sql_insert_alert = f"""
                        INSERT INTO public.alerts (
                            portfolio_id, listing_id, ticker_id, source_type, broker_alert_id,
                            ticker, init_price, trigger_price_raw, trigger_price, condition_type,
                            quote_type, notification_type, trigger_type, periodic, expire_raw,
                            triggered_status, deleted_status, is_active, trigger_price_min,
                            trigger_price_max, trigger_pct, expire_type, expire_at, triggered_at, 
                            created_by_user_id, created_at, updated_at
                        ) VALUES (
                            {int(portfolio_id)}, {int(al_listing_id)}, {int(al_ticker_id)}, 'FB', {b_alert_id},
                            {sql_val(b_ticker)}, {init_price}, {sql_val(str(raw_trig_price))}, {trigger_price}, {sql_val(condition)},
                            {sql_val(b_al.get('quote_type', 'ltp'))}, {sql_val(b_al.get('notification_type', 'push'))},
                            {sql_val(t_type)}, {periodic}, {sql_val(expire_raw)}, {sql_val(str(is_trig))}, '0', {sql_val(uport_active)}, 
                            {sql_val(trigger_price_min)}, {sql_val(trigger_price_max)}, {sql_val(trigger_pct)},
                            {sql_val(expire_type)}, {sql_val(expire_at)}, {sql_val(triggered_at)}, {sql_val(user_id)},
                            (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0), (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                        );
                    """
                    db_sys.execute_query(sql_insert_alert)
                    logging.info(f"➕ [SYNC ALERTS]: Впервые создан паспорт алерта #{b_alert_id} для {b_ticker}")

        # ─── ШАГ 5: СЛУЖБА ПАКЕТНОГО ГАШЕНИЯ УДАЛЕННЫХ АЛЕРТОВ (БЕЗ КОСТЫЛЕЙ) ───
        # Переводим статус в false строго для тех алертов, которые инвестор физически стер в терминале брокера
        if synced_alert_ids:
            active_ids_str = ",".join(map(str, synced_alert_ids))
            
            sql_deactivate_removed = f"""
                UPDATE public.alerts 
                SET is_active = false,
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                WHERE portfolio_id = {int(portfolio_id)}
                  AND source_type = 'FB'
                  AND broker_alert_id NOT IN ({active_ids_str});
            """
            try:
                db_sys.execute_query(sql_deactivate_removed)
                logging.info(f"🧹 [SYNC ALERTS]: Выполнена пакетная деактивация удаленных алертов для портфеля ID {portfolio_id}")
            except Exception as clean_err:
                logging.error(f"❌ Ошибка пакетной деактивации (NOT IN) для portfolio_id {portfolio_id}: {clean_err}")
        else:
            # Если у брокера стало ноль активных алертов, тотально выключаем все записи этого портфеля
            sql_deactivate_all = f"""
                UPDATE public.alerts 
                SET is_active = false,
                    updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
                WHERE portfolio_id = {int(portfolio_id)}
                  AND source_type = 'FB';
            """
            try:
                db_sys.execute_query(sql_deactivate_all)
                logging.info(f"🧹 [SYNC ALERTS]: Тотально выключены все алерты для портфеля ID {portfolio_id} (0 на бирже)")
            except Exception as clean_all_err:
                logging.error(f"❌ Ошибка тотального выключения для portfolio_id {portfolio_id}: {clean_all_err}")

        logging.info(f"✅ Синхронизация портфеля '{portfolio_name}' успешно завершена.\n")

    logging.info("⚡ [UPort ALERTS WORKER]: Общий контур синхронизации алертов успешно отработал.")

if __name__ == "__main__":
    sync_all_broker_alerts()
