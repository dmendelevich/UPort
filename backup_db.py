#!/usr/bin/env python3
"""
Отдельно запускаемый скрипт (не часть живого процесса бота) -- снимает полный
pg_dump БД (schema+data вместе, custom-формат) в локальную папку на дроплете.
Запускается системным cron ежедневно, а также вручную сразу после тем, где
менялась схема. Claude/18_db_backup_strategy_prep.md -- принципы и решения.
"""
import os
import sys
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

BACKUP_DIR = Path("/root/db_backups/uport")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    load_dotenv(dotenv_path=Path(__file__).parent.resolve() / ".env")

    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASS")

    if not all([db_host, db_port, db_name, db_user, db_pass]):
        logging.error("Не все DB_* переменные заданы в .env -- бэкап невозможен.")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = BACKUP_DIR / f"uport_{timestamp}.dump"

    env = os.environ.copy()
    env["PGPASSWORD"] = db_pass

    result = subprocess.run(
        ["pg_dump", "-Fc", "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name, "-f", str(out_path)],
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logging.error(f"pg_dump завершился с ошибкой: {result.stderr.strip()}")
        if out_path.exists():
            out_path.unlink()
        sys.exit(1)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    logging.info(f"Бэкап создан: {out_path.name} ({size_mb:.2f} МБ)")
