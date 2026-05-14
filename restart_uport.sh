#!/bin/bash

echo "🔄 Перезапуск всех сервисов UPORT..."

# 1. Перезапуск Шлюза API
sudo systemctl restart uport_gateway
echo "✅ Gateway перезапущен"

# 2. Перезапуск Телеграм-бота
sudo systemctl restart uport_bot
echo "✅ Telegram Bot перезапущен"

# 3. Перезапуск Слушателей Почты (Временно только DLM, пока нет данных для EAM и MDM)
FAMILY_MEMBERS=("dlm")

# Гарантированно глушим старую нешаблонную службу, если она осталась
sudo systemctl stop uport_emails 2>/dev/null
sudo systemctl disable uport_emails 2>/dev/null

for member in "${FAMILY_MEMBERS[@]}"; do
    sudo systemctl enable uport_emails@$member 2>/dev/null
    sudo systemctl restart uport_emails@$member
    echo "✅ Email Listener для [$member] перезапущен"
done

echo "-----------------------------------"
echo "📊 Статус сервисов:"

# Проверка базовых служб
BASE_SERVICES=("uport_gateway" "uport_bot")
for service in "${BASE_SERVICES[@]}"; do
    STATUS=$(systemctl is-active $service)
    if [ "$STATUS" = "active" ]; then
        echo "🟢 $service: Работает (active)"
    else
        echo "🔴 $service: ОШИБКА ($STATUS)"
    fi
done

# Проверка активных почтовых шаблонов
for member in "${FAMILY_MEMBERS[@]}"; do
    STATUS=$(systemctl is-active uport_emails@$member)
    if [ "$STATUS" = "active" ]; then
        echo "🟢 uport_emails@$member: Работает (active)"
    else
        echo "🔴 uport_emails@$member: ОШИБКА ($STATUS)"
    fi
done
echo "-----------------------------------"
