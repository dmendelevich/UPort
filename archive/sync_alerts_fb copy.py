import os
import sys
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import logging

# Настройка путей для запуска как демона по крону
sys.path.append(str(Path(__file__).parent.parent))

from database import Database
from brokers_connectors.fb_client import FreedomBrokerClient

def sync_all_broker_alerts():
    print(f"\n📡 [URGENT ALERTS WORKER]: Старт контура синхронизации алертов Freedom Broker... ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    
    # 1. Инициализируем подключение к СУБД с ролью SYSTEM (максимальные права)
    db = Database(role="SYSTEM")
    
    # 2. Выгребаем из базы данных все ТОРГОВЫЕ счета Freedom Broker семьи
    accounts_sql = """
        SELECT DISTINCT ON (a.account_number) a.account_number, a.portfolio_id, u.prefix 
        FROM public.accounts a
        JOIN public.users u ON a.user_id = u.id
        WHERE a.broker_id = 1 AND a.account_type = 'trade'
        ORDER BY a.account_number, a.id ASC;
    """    
    trade_accounts = db.execute_query(accounts_sql)
    if not trade_accounts:
        print("💡 [SYNC ALERTS]: Активных торговых аккаунтов Freedom Broker в базе не найдено.")
        return

    accounts_list = trade_accounts if isinstance(trade_accounts, list) else [trade_accounts]

    # Для прямой работы с транзакциями открываем нативное psycopg2-подключение
    db_params = {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "database": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASS")
    }

    try:
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        for acc in accounts_list:
            acc_num = acc['account_number']
            portfolio_id = acc['portfolio_id']
            prefix = acc['prefix']
            
            if not portfolio_id:
                continue
                
            print(f"🔐 [SYNC ALERTS]: Авторизация для субсчета {acc_num} ({prefix})...")
            api_key = os.getenv(f"FB_{prefix}_API_KEY")
            api_secret = os.getenv(f"FB_{prefix}_API_SECRET")
            
            if not api_key or not api_secret:
                print(f"⚠️ Предупреждение: Ключи для префикса FB_{prefix}_API_KEY не найдены в .env. Пропускаю.")
                continue
                
            try:
                fb_client = FreedomBrokerClient(public_key=api_key, private_key=api_secret)
                broker_alerts = fb_client.get_active_alerts()
                
                if not broker_alerts:
                    print(f"🕊️ [SYNC ALERTS]: На счете {acc_num} нет активных алертов у брокера.")
                    continue
                    
                print(f"📥 [SYNC ALERTS]: Получено {len(broker_alerts)} алертов. Начинаю реляционную обработку...")
                
                synced_alert_ids = []
                for b_al in broker_alerts:
                    b_ticker = b_al.get('ticker')
                    b_alert_id = int(b_al.get('id', 0))
                    auth_login = b_al.get('auth_login', '')
                    
                    if not b_ticker or not b_alert_id:
                        continue
                    synced_alert_ids.append(b_alert_id)

                    # 🔥 РЕШЕНИЕ ГОНКИ ДАННЫХ: Кит-метод лелегализует тикер на лету
                    # 🔥 ИСПРАВЛЕНО v3.0 (ЭТАП 1): Переключаем алерты брокера на универсальные Главные Ворота.
                    # Роль LST, автограф провенанса сгенерируется СУБД автоматически на базе листинга.
                    al_ticker_id, al_listing_id = db.ensure_ticker_v3(
                        ticker_name_raw=b_ticker, 
                        caller_role="LST", 
                        caller_id=None,
                        broker_id=1, 
                        fb_client=fb_client
                    )

                    # 🔥 АВТОЛЕГАЛИЗАЦИЯ WATCHLIST: Прописываем бумагу в фокус стратегии портфеля
                    # 🔥 ИСПРАВЛЕНО v3.0 (ПАКЕТ 2): ЛИКВИДАЦИЯ ГЕНЕРАТОРА МЕГА-ID (ЗАЩИТА СПИДОМЕТРА СУБД)
                    # Вместо слепого инсерта с конфликтом сначала делаем зрячую проверку существования записи.
                    cur.execute("""
                        SELECT id FROM public.watchlist 
                        WHERE portfolio_id = %s AND listing_id = %s LIMIT 1;
                    """, (portfolio_id, al_listing_id))
                    wl_row = cur.fetchone()

                    if wl_row:
                        # А) Строка уже есть — счетчик СУБД НЕ ТРОГАЕМ! Делаем тихий, бесшумный апдейт времени
                        wl_id = int(wl_row['id'])
                        cur.execute("""
                            UPDATE public.watchlist 
                            SET updated_at = CURRENT_TIMESTAMP 
                            WHERE id = %s;
                        """, (wl_id,))
                    else:
                        # Б) Строки нет (бумага новая) — делаем честный, чистый INSERT без накрутки
                        cur.execute("""
                            INSERT INTO public.watchlist (portfolio_id, listing_id, considered_at, watched_at)
                            VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                        """, (portfolio_id, al_listing_id))

                    # Шаг А: Реляция Инициатора (Связка через Email)
                    cur.execute("SELECT id FROM public.users WHERE email = %s LIMIT 1;", (auth_login,))
                    u_row = cur.fetchone()
                    created_by_user_id = int(u_row['id']) if u_row else None

                    # Шаг Б: Снайперский разбор Сроков (expire)
                    expire_raw = str(b_al.get('expire', '0')).strip()
                    expire_type = 'good_till_cancelled'
                    expire_at = None

                    # 🔥 ИСПРАВЛЕНО v3.0 (ЭТАП 1): Переводим все генераторы времени алертов на стерильный UTC-стандарт
                    if expire_raw == 'end_of_day':
                        expire_type = 'end_of_day'
                        # Заменяем datetime.now() на datetime.now(timezone.utc)
                        expire_at = datetime.now(timezone.utc).replace(hour=23, minute=59, second=58, microsecond=0)
                    elif expire_raw != '0' and expire_raw != '':
                        expire_type = 'good_till_date'
                        if expire_raw.isdigit():
                            try:
                                expire_at = datetime.fromtimestamp(int(expire_raw), tz=timezone.utc) # Добавляем tz сюда
                            except Exception:
                                expire_at = None
                        else:
                            try:
                                clean_date_str = expire_raw.split('.')
                                expire_at = datetime.strptime(clean_date_str[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            except Exception:
                                expire_at = None

                    # Шаг В: Распаковка сложных цен и каналов из JSON
                    raw_trig_price = b_al.get('trigger_price', '{}')
                    trig_data = json.loads(raw_trig_price) if isinstance(raw_trig_price, str) else raw_trig_price
                    
                    trigger_price = 0.0
                    trigger_price_min = None
                    trigger_price_max = None
                    trigger_pct = None

                    if 'price' in trig_data:
                        trigger_price = float(trig_data.get('price', 0))
                    elif 'min_price' in trig_data or 'max_price' in trig_data:
                        trigger_price_min = float(trig_data.get('min_price', 0)) if trig_data.get('min_price') else None
                        trigger_price_max = float(trig_data.get('max_price', 0)) if trig_data.get('max_price') else None
                    elif 'pct' in trig_data:
                        trigger_pct = float(trig_data.get('pct', 0))

                    # Вычисляем математический знак условия (< или >)
                    init_p = float(b_al.get('init_price') or 0)
                    t_type = b_al.get('trigger_type', 'crossing')
                    
                    if t_type in ('crossing_down', 'less_then'):
                        condition = '<'
                    elif t_type in ('crossing_up', 'greater_then'):
                        condition = '>'
                    else:
                        condition = '<' if (trigger_price and trigger_price < init_p) else '>'

                    # Шаг Г: Управление Честным Временем Срабатывания
                    is_trig = b_al.get('triggered', '')
                    is_del = b_al.get('deleted', '0')
                    
                    broker_says_inactive = (is_trig and str(is_trig).strip()) or is_del == '1'

                    # Смотрим текущий статус в СУБД UPort
                    cur.execute("""
                        SELECT is_active, triggered_at FROM public.alerts 
                        WHERE portfolio_id = %s AND broker_alert_id = %s LIMIT 1;
                    """, (portfolio_id, b_alert_id))
                    db_alert = cur.fetchone()

                    uport_active = True
                    triggered_at = None

                    if db_alert:
                        if broker_says_inactive:
                            uport_active = False
                            if db_alert['is_active'] is True:
                                # Заменяем datetime.now() на datetime.now(timezone.utc)
                                triggered_at = datetime.now(timezone.utc)
                                print(f"🔥 [ТРИГГЕР]: Боевой алерт #{b_alert_id} ({b_ticker}) сработал на бирже!")
                            else:
                                triggered_at = db_alert['triggered_at']
                    else:
                        if broker_says_inactive:
                            uport_active = False
                            # Заменяем datetime.now() на datetime.now(timezone.utc)
                            triggered_at = datetime.now(timezone.utc)
                            
                    # 🔥 ИСПРАВЛЕНО v3.0 (ПАКЕТ АЛЕРТОВ): ЛЛИКВИДАЦИЯ ГЕНЕРАТОРА МЕГА-ID (ЗАЩИТА СПИДОМЕТРА)
                    # Вместо слепого инсерта с конфликтом сначала делаем зрячую проверку существования алерта.
                    cur.execute("""
                        SELECT id FROM public.alerts 
                        WHERE portfolio_id = %s AND broker_alert_id = %s LIMIT 1;
                    """, (portfolio_id, b_alert_id))
                    al_row = cur.fetchone()

                    if al_row:
                        # А) Алерт уже есть — счетчик СУБД НЕ ТРОГАЕМ! Делаем тихий, бесшумный апдейт параметров
                        al_db_id = int(al_row['id'])
                        sql_update_alert = """
                            UPDATE public.alerts SET
                                trigger_price = %s,
                                trigger_price_min = %s,
                                trigger_price_max = %s,
                                trigger_pct = %s,
                                expire_type = %s,
                                expire_at = %s,
                                triggered_status = %s,
                                deleted_status = %s,
                                is_active = %s,
                                triggered_at = %s,
                                updated_at = CURRENT_TIMESTAMP(0)
                            WHERE id = %s;
                        """
                        cur.execute(sql_update_alert, (
                            trigger_price, trigger_price_min, trigger_price_max, trigger_pct,
                            expire_type, expire_at, str(is_trig), str(is_del), uport_active, triggered_at,
                            al_db_id
                        ))
                    else:
                        # Б) Алерта нет (он абсолютно новый) — делаем честный, чистый INSERT без накрутки
                        sql_insert_alert = """
                            INSERT INTO public.alerts (
                                portfolio_id, listing_id, source_type, broker_alert_id, auth_login,
                                ticker, init_price, trigger_price_raw, trigger_price, condition_type,
                                quote_type, notification_type, trigger_type, periodic, expire_raw,
                                triggered_status, deleted_status, is_active,
                                trigger_price_min, trigger_price_max, trigger_pct, expire_type, expire_at, triggered_at, created_by_user_id
                            ) VALUES (
                                %s, %s, 'broker', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s
                            );
                        """
                        cur.execute(sql_insert_alert, (
                            portfolio_id, al_listing_id, b_alert_id, auth_login, b_ticker, init_p, 
                            str(raw_trig_price), trigger_price, condition, b_al.get('quote_type', 'ltp'), 
                            b_al.get('notification_type', 'push'), t_type, int(b_al.get('periodic') or 0), 
                            expire_raw, str(is_trig), str(is_del), uport_active,
                            trigger_price_min, trigger_price_max, trigger_pct, expire_type, expire_at, triggered_at, created_by_user_id
                        ))

                # 🔥 ИСПРАВЛЕНО v3.0 (ПАКЕТ СНЯТИЯ): Автоматическое обнаружение удаленных алертов.
                # Если у нас набрался список живых алертов от брокера, мы пакетно находим все старые 
                # алерты этого портфеля, которые брокер НЕ прислал, и взводим им deleted_status = '1'.
                if synced_alert_ids:
                    # Превращаем питоновский список [2601912, 2709160] в SQL-строку "2601912,2709160"
                    active_ids_str = ",".join(map(str, synced_alert_ids))
                    
                    sql_deactivate_removed = f"""
                        UPDATE public.alerts 
                        SET deleted_status = '1',
                            updated_at = CURRENT_TIMESTAMP(0)
                        WHERE portfolio_id = %s 
                          AND source_type = 'broker'
                          AND broker_alert_id NOT IN ({active_ids_str});
                    """
                    cur.execute(sql_deactivate_removed, (portfolio_id,))
                    logging.info(f"🧹 [SYNC ALERTS]: Пакетно деактивированы снятые алерты для portfolio_id = {portfolio_id}")


                print(f"✅ [SYNC ALERTS]: Субсчет {acc_num} успешно синхронизирован.")
                
            except Exception as account_err:
                print(f"❌ [SYNC ALERTS ERROR] Сбой обработки аккаунта {acc_num}: {account_err}")
        
        conn.commit()
        print("⚡ [SYNC ALERTS]: Общая транзакция успешно зафиксирована в СУБД.")

    except Exception as global_db_err:
        if 'conn' in locals() and conn: conn.rollback()
        print(f"❌ [SYNC ALERTS CRITICAL]: Критический сбой СУБД: {global_db_err}")
    finally:
        if 'cur' in locals() and cur: cur.close()
        if 'conn' in locals() and conn: conn.close()

if __name__ == "__main__":
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    sync_all_broker_alerts()
