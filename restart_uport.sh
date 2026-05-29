#!/bin/bash

echo "=== Systemd: Чистый и БЫСТРЫЙ перезапуск монолита UPort ==="

# Командуем Linux перезапустить службу. ОС сама за 2 секунды очистит сокеты и поднимет новый код.
sudo systemctl restart uport.service

echo "✅ Монолит UPort успешно перезапущен!"
echo "Для просмотра живого лога используйте команду: tail -f uport_system.log"
