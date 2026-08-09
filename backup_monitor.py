#!/usr/bin/env python3
"""
Отдельно запускаемый скрипт (не часть живого процесса бота, не зависит от
БД/шлюза -- именно на случай их поломки) -- проверяет, что бэкап БД не
протух: (1) свежесть последнего дампа на дроплете, (2) свежесть маркера
синхронизации на ноутбук. Алерт шлётся напрямую в Telegram Bot API по
захардкоженному в .env chat_id. Запускается системным cron несколько раз
в сутки. Claude/18_db_backup_strategy_prep.md -- принципы и решения.
"""
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

BACKUP_DIR = Path("/root/db_backups/uport")
DUMP_STALE_HOURS = 26
SYNC_STALE_HOURS = 48

DUMP_ALERT_SENT = BACKUP_DIR / ".dump_alert_sent"
SYNC_ALERT_SENT = BACKUP_DIR / ".sync_alert_sent"
SYNC_MARKER = BACKUP_DIR / ".last_synced_ok"


def _age_hours(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600


def _latest_dump_age_hours() -> Optional[float]:
    dumps = list(BACKUP_DIR.glob("uport_*.dump"))
    if not dumps:
        return None
    latest = max(dumps, key=lambda p: p.stat().st_mtime)
    return _age_hours(latest)


def _send_telegram_alert(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    if resp.status_code != 200:
        logging.error(f"Не удалось отправить алерт в Telegram: {resp.status_code} {resp.text}")


def _check(label: str, age_hours: Optional[float], threshold_hours: int, sent_marker: Path,
           token: str, chat_id: str, missing_text: str) -> None:
    is_stale = age_hours is None or age_hours > threshold_hours
    if is_stale:
        if not sent_marker.exists():
            text = missing_text if age_hours is None else (
                f"⚠️ UPort: {label} не обновлялся {age_hours:.1f}ч (порог {threshold_hours}ч)."
            )
            _send_telegram_alert(token, chat_id, text)
            sent_marker.touch()
    else:
        if sent_marker.exists():
            sent_marker.unlink()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    load_dotenv(dotenv_path=Path(__file__).parent.resolve() / ".env")

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("BACKUP_ALERT_TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logging.error(
            "TELEGRAM_TOKEN / BACKUP_ALERT_TELEGRAM_CHAT_ID не заданы в .env -- "
            "мониторинг бэкапа не может слать алерты."
        )
        raise SystemExit(1)

    _check(
        "дамп БД на дроплете",
        _latest_dump_age_hours(),
        DUMP_STALE_HOURS,
        DUMP_ALERT_SENT,
        token,
        chat_id,
        missing_text="🔴 UPort: в /root/db_backups/uport/ вообще нет ни одного файла дампа БД.",
    )
    _check(
        "синхронизация бэкапа на ноутбук",
        _age_hours(SYNC_MARKER),
        SYNC_STALE_HOURS,
        SYNC_ALERT_SENT,
        token,
        chat_id,
        missing_text="⚠️ UPort: бэкап БД ни разу не синхронизировался на ноутбук (нет маркер-файла).",
    )
