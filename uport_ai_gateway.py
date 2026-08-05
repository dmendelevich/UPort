import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
import psycopg2
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path

# Загружаем переменные из .env
env_path = Path('/root/UPort/.env')
load_dotenv(dotenv_path=env_path)

app = FastAPI()

# Конфигурация технических токенов безопасности
TOKEN_SYSTEM = os.getenv("UPORT_TOKEN_SYSTEM", "uport_sys_secret")
TOKEN_AI = os.getenv("UPORT_TOKEN_AI", "uport_ai_secret")
TOKEN_BOT = os.getenv("UPORT_TOKEN_BOT", "uport_bot_secret")

# Динамическая конфигурация подключения к PostgreSQL из вашего .env
DB_PARAMS = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "database": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS")
}

class QueryPayload(BaseModel):
    query: str
    params: Optional[list] = None

class TransactionPayload(BaseModel):
    statements: list[QueryPayload]

def _identify_role(x_token: str) -> str:
    if x_token == TOKEN_SYSTEM:
        return "SYSTEM"
    elif x_token == TOKEN_AI:
        return "AI"
    elif x_token == TOKEN_BOT:
        return "BOT"
    raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing X-Token")

def _verify_role_permission(role: str, query: str):
    """Общая проверка прав роли -- используется и /query, и /transaction (Claude/BACKLOG.md №81),
    чтобы не разъезжались правила между одиночным запросом и пакетом внутри транзакции."""
    query_lower = query.strip().lower()

    if role == "AI":
        if not query_lower.startswith("select"):
            raise HTTPException(
                status_code=403,
                detail="Forbidden for AI: Only SELECT queries are allowed via this token."
            )

    elif role == "BOT":
        allowed_bot_commands = ("select", "insert", "update")
        if not query_lower.startswith(allowed_bot_commands):
            raise HTTPException(status_code=403, detail="Forbidden for BOT: Operation not allowed.")

        if (query_lower.startswith("insert") or query_lower.startswith("update")) and \
           any(x in query_lower for x in ["assets", "tickers", "orders", "transactions"]):
            raise HTTPException(
                status_code=403,
                detail="Forbidden for BOT: Direct asset/order modification is restricted."
            )

    elif role == "SYSTEM":
        # Полный доступ ко всем финансовым таблицам (assets, tickers, orders)
        pass

@app.get("/")
def read_root():
    return {
        "message": "UPort AI Gateway is Online",
        "version": "3.2 (Parameterized queries + atomic transactions)"
    }

@app.post("/query")
def execute_custom_query(payload: QueryPayload, x_token: str = Header(None)):
    role = _identify_role(x_token)
    _verify_role_permission(role, payload.query)

    # Выполнение запроса в PostgreSQL. params=None у psycopg2 равносилен обычному execute(query) --
    # старые вызовы без параметров (f-строка целиком в query) продолжают работать без изменений,
    # пока кодовая база постепенно переходит на параметризованный стиль (Claude/BACKLOG.md №81).
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(payload.query, payload.params)

        # cur.description -- факт от Postgres "есть ли у этого запроса набор колонок результата".
        # В отличие от query_lower.startswith("select"), корректно работает и для WITH...SELECT (CTE),
        # и для INSERT/UPDATE...RETURNING, которые тоже возвращают строки, не начинаясь со слова SELECT.
        if cur.description is not None:
            result = cur.fetchall()
        else:
            result = [{"status": "success", "message": f"Command executed successfully via {role} role"}]

        # Коммитим всегда: и для чистых SELECT (безвредно), и для RETURNING-запросов, которые
        # одновременно меняют данные И возвращают строки -- иначе изменения откатятся при conn.close().
        conn.commit()

        cur.close()
        conn.close()
        return result
    except Exception as e:
        if 'conn' in locals() and conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/transaction")
def execute_transaction(payload: TransactionPayload, x_token: str = Header(None)):
    """
    Честная атомарная транзакция поверх шлюза (Claude/BACKLOG.md №81) -- корень проблемы,
    из-за которой раньше приходилось открывать psycopg2.connect в обход шлюза напрямую:
    /query открывает НОВОЕ соединение на каждый HTTP-вызов, сессии между вызовами не существует,
    поэтому BEGIN/COMMIT отдельными вызовами execute_query() никогда не давали реальной транзакции.
    Здесь -- одно соединение на весь список запросов, коммит в конце ИЛИ полный откат при любой
    ошибке любого из шагов.
    """
    role = _identify_role(x_token)
    for stmt in payload.statements:
        _verify_role_permission(role, stmt.query)

    conn = None
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        results = []
        for stmt in payload.statements:
            cur.execute(stmt.query, stmt.params)
            if cur.description is not None:
                results.append(cur.fetchall())
            else:
                results.append([{"status": "success", "message": f"Command executed successfully via {role} role"}])

        conn.commit()
        cur.close()
        conn.close()
        return results
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        raise HTTPException(status_code=500, detail=str(e))
