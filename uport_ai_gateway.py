import os
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

@app.get("/")
def read_root():
    return {
        "message": "UPort AI Gateway is Online", 
        "version": "3.1 (Env-Configured RBAC)"
    }

@app.post("/query")
def execute_custom_query(payload: QueryPayload, x_token: str = Header(None)):
    # Идентификация системной роли
    if x_token == TOKEN_SYSTEM:
        role = "SYSTEM"
    elif x_token == TOKEN_AI:
        role = "AI"
    elif x_token == TOKEN_BOT:
        role = "BOT"
    else:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing X-Token")
    
    query = payload.query
    query_lower = query.strip().lower()
    
    # Верификация прав роли
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

    # Выполнение запроса в PostgreSQL
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query)
        
        if query_lower.startswith("select"):
            result = cur.fetchall()
        else:
            conn.commit()
            result = [{"status": "success", "message": f"Command executed successfully via {role} role"}]
            
        cur.close()
        conn.close()
        return result
    except Exception as e:
        if 'conn' in locals() and conn: 
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
