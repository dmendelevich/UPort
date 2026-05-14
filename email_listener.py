import os
import sys
import re
import logging
from imap_tools import MailBox, A
from dotenv import load_dotenv
from pathlib import Path

# Импортируем компоненты синхронизации
from database import Database
from brokers_connectors.fb_client import FreedomBrokerClient
from brokers_connectors.fb_sync_manager import FreedomBrokerSyncManager

logging.basicConfig(level=logging.INFO)

def main():
    if len(sys.argv) < 2:
        print("❌ ERROR: Не указан префикс владельца (Например: python3 email_listener.py DLM)")
        sys.exit(1)
        
    # ИСПРАВЛЕНО: строгое извлечение первого аргумента [1] перед переводом в верхний регистр
    owner_prefix = sys.argv[1].upper()
    
    env_path = Path('/root/UPort/.env')
    load_dotenv(dotenv_path=env_path)
    
    imap_server = os.getenv(f"EMAIL_IMAP_SERVER_{owner_prefix}")
    email_user = os.getenv(f"EMAIL_USER_{owner_prefix}")
    email_pass = os.getenv(f"EMAIL_PASS_{owner_prefix}")
    
    if not all([imap_server, email_user, email_pass]):
        print(f"❌ ERROR: Конфигурация для EMAIL_{owner_prefix} не заполнена в .env.")
        sys.exit(1)
        
    db_sys = Database(role="SYSTEM")
    sync_manager = FreedomBrokerSyncManager(db_instance=db_sys, fb_client_class=FreedomBrokerClient)

    # Регулярные выражения
    account_pattern = r"\b(D?[A-Z0-9]{7})\b"
    trade_pattern = r"(Покупка|Продажа)\s+(\d+)\s+шт\.\s+([\w\.]+)\s+.*по\s+([\d\.]+)"
    order_pattern = r"([\w\.]+)\s+(\d+)\s+шт\.(?:\s+по\s+([\d\.]+))?\s*на\s+(покупку|продажу)\s+был\s+(размещен|снят)\s+([\w\s]+)\s+(\d+)"

    print(f"👂 [Служба EMAIL_{owner_prefix}] Мониторинг запущен для: {email_user}")

    with MailBox(imap_server).login(email_user, email_pass) as mailbox:
        while True:
            mailbox.idle.wait(timeout=60)
            
            for msg in mailbox.fetch(A(seen=False)):
                from_lower = msg.from_.lower()
                
                if 'tradernet' in from_lower or 'freedom' in from_lower:
                    subject = msg.subject
                    
                    # Наша проверенная очистка HTML из парсера
                    raw_body = msg.text if msg.text else ""
                    if not raw_body and msg.html:
                        raw_body = re.sub('<[^<]+?>', '', msg.html)
                    
                    # Поиск номера счета (сначала в теме, затем в очищенном теле письма)
                    acc_match = re.search(account_pattern, subject)
                    if not acc_match:
                        acc_match = re.search(account_pattern, raw_body)
                        
                    if not acc_match:
                        logging.warning(f"⚠️ Предупреждение: В письме '{subject}' не обнаружен брокерский счет.")
                        mailbox.flag(msg.uid, '\\Seen', True)
                        continue
                        
                    account_number = acc_match.group(1)
                    
                    # Поиск повода
                    is_trade = re.search(trade_pattern, subject)
                    is_order = re.search(order_pattern, subject)
                    is_money = "счет" in subject.lower() and "пополнен" in subject.lower()
                    
                    if is_trade or is_order or is_money:
                        p_type = "Сделка" if is_trade else ("Приказ" if is_order else "Движение Кэша")
                        print(f"🔔 [Триггер {owner_prefix}] Обнаружен повод: '{p_type}' по счету {account_number}. Запуск API...")
                        
                        try:
                            res = sync_manager.sync_by_account_number(account_number)
                            print(f"🚀 [Успех API] Владелец: {res['owner_name']}, Счет: {res['account_number']} ({res['account_type']}). Синхронизировано акций: {res['synced_assets']}")
                        except Exception as e:
                            logging.error(f"❌ Ошибка фонового обновления по API для счета {account_number}: {e}")
                    
                    mailbox.flag(msg.uid, '\\Seen', True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Работа почтового триггера остановлена.")
