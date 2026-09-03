#!/usr/bin/env python3
"""
Демон «ПБумКлод» (Claude/BACKLOG.md №158/164) -- headless-агент на Claude Agent
SDK, запускается по расписанию (systemd-таймер, не часть основного uport.service).
Не хранит собственную БД-логику -- вся торговая механика уже в analytics/
claude_paper_trader.py (buy/sell/report), этот файл только даёт агенту три
инструмента поверх нёе и оформляет вызов + доставку итога в Telegram.

Мандат (согласован 2026-09-01, BACKLOG.md №158) -- зашит в system_prompt ниже,
не выводится заново при каждом запуске: лимиты риска как у Р/К/Т (самопроверка
через report(), не блокирующий гейт), тезис обязателен при покупке, выход --
целиком суждение агента, без механического бэкстопа, без предохранителя на
просадку портфеля.

permission_mode="dontAsk" (не "bypassPermissions" -- тот заблокирован под root
по соображениям безопасности) -- разрешённые инструменты объявлены явно и узко
(три обёртки над claude_paper_trader.py + WebSearch), не Bash/Read/Write --
агент физически не может сделать ничего, кроме того, что эти три функции
позволяют.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).parent.resolve()))
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path('/root/UPort/.env'))

# SDK -- жёсткий таймаут control-request "initialize" (по умолчанию 60 сек, живая
# находка 2026-09-02 -- на этом сервере поднятие сессии с MCP-сервером иногда занимает
# больше). Переменная окружения самого SDK, читается ДО импорта claude_agent_sdk.
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "180000")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [ПБумКлод-агент] %(message)s')

from claude_agent_sdk import query, tool, create_sdk_mcp_server, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage

# ЛЕНИВЫЙ импорт UPort-стека (database/claude_paper_trader тянут за собой yfinance
# через execution_price_advisor.py) -- на уровне модуля этот импорт занимает 25-60+ сек
# и срывает control-request "initialize" у SDK (жёсткий таймаут на старте сессии,
# живая находка 2026-09-02). Импортируем внутри функций, ПОСЛЕ того как сессия SDK
# уже поднялась -- к моменту первого реального вызова инструмента время импорта уже
# не конкурирует за критичное окно инициализации.
PORTFOLIO_ID = 20  # ПБумКлод, analytics.claude_paper_trader.CLAUDE_PORTFOLIO_ID
OWNER_TELEGRAM_ID = 250720161  # dmend, owner_id=1 -- см. Claude/BACKLOG.md №158


def _fmt_report(data: dict) -> str:
    lines = [f"Свободный кэш: ${data['cash_available']:,.2f}"]
    if not data["holdings"]:
        lines.append("Держимых позиций нет.")
    else:
        lines.append("Держимые позиции:")
        for h in data["holdings"]:
            pnl_pct = (float(h["last_price"]) - float(h["avg_price"])) / float(h["avg_price"]) * 100 if h.get("avg_price") else 0
            lines.append(
                f"- {h['symbol']} (listing_id={h['listing_id']}): {h['quantity']:g} шт по ${h['avg_price']:.2f}, "
                f"сейчас ${h['last_price']:.2f} ({pnl_pct:+.1f}%). "
                f"Тезис: {h.get('thesis') or '(нет записи -- расхождение, стоит отметить)'}"
            )
            if h.get("exit_criteria"):
                lines.append(f"  Критерий выхода: {h['exit_criteria']}")
    pending = data.get("pending_orders") or []
    if pending:
        lines.append("Заявки, ожидающие исполнения (решение уже принято, ждут открытия рынка -- НЕ дублируй их новой покупкой/продажей):")
        for p in pending:
            qty = float(p["target_quantity"])
            direction = "купить" if qty > 0 else "продать"
            lines.append(f"- {direction} {p['symbol']}: {abs(qty):g} шт по ~${float(p['initial_entry_price'] or 0):.2f}")
    audit = data.get("limits_audit") or {}
    lines.append(f"Нарушение лимитов риска: {'ДА' if audit.get('has_violations') else 'нет'}")
    if audit.get("has_violations"):
        lines.append(str(audit.get("strategies")))
    return "\n".join(lines)


@tool("portfolio_report", "Текущее состояние ПБумКлод: держимые позиции, тезис по каждой, свободный кэш, отчёт по лимитам риска (5%% на бумагу / 25%% на сектор). Вызывай в начале каждой сессии.", {})
async def portfolio_report_tool(args):
    # Прямой (не asyncio.to_thread) синхронный вызов -- живая находка 2026-09-02:
    # обёртка в to_thread здесь зависала намертво (SDK ждёт ответ инструмента
    # бесконечно), видимо, конфликт с собственным asyncio-транспортом SDK к
    # дочернему процессу CLI. Сама БД-функция быстрая (секунды), приемлемо
    # ненадолго блокировать луп на единственном пользователе этого демона.
    from database import db_sys
    from analytics import claude_paper_trader as cpt
    data = cpt.report(db_sys, PORTFOLIO_ID)
    return {"content": [{"type": "text", "text": _fmt_report(data)}]}


@tool("buy_stock", "Купить акцию/фонд США в ПБумКлод. Тезис ОБЯЗАТЕЛЕН -- без него сделка не создастся.",
      {"ticker": str, "amount_usd": float, "thesis": str, "exit_criteria": str})
async def buy_stock_tool(args):
    from database import db_sys
    from analytics import claude_paper_trader as cpt
    result = cpt.buy(db_sys, args["ticker"], float(args["amount_usd"]), args["thesis"],
                      args.get("exit_criteria"), PORTFOLIO_ID)
    return {"content": [{"type": "text", "text": str(result)}]}


@tool("sell_stock", "Продать держимую позицию ПБумКлод целиком. Укажи listing_id из portfolio_report.",
      {"listing_id": int, "close_reason": str})
async def sell_stock_tool(args):
    from database import db_sys
    from analytics import claude_paper_trader as cpt
    result = cpt.sell(db_sys, int(args["listing_id"]), args["close_reason"], PORTFOLIO_ID)
    return {"content": [{"type": "text", "text": str(result)}]}


server = create_sdk_mcp_server(
    name="pbumklod", version="1.0.0",
    tools=[portfolio_report_tool, buy_stock_tool, sell_stock_tool],
)

SYSTEM_PROMPT = """Ты управляешь бумажным (не реальным) портфелем «ПБумКлод» в системе UPort -- $50 000 стартового капитала, дискреционные решения только твои, не по правилам механических стратегий.

Мандат (согласован с владельцем портфеля 2026-09-01):
- Только акции и фонды США (никаких других инструментов).
- Работай так, как если бы брокером был Freedom Broker (только рынок США).
- Можно покупать ЛЮБОЙ американский тикер, не только те, что уже есть в базе UPort -- инструмент buy_stock сам легализует новый тикер при необходимости.
- Лимиты риска -- как у механических стратегий системы (5% капитала на одну бумагу, 25% на сектор). Это НЕ блокирующий гейт -- self-check: посмотри audit в portfolio_report ПЕРЕД тем, как размер новой позиции, сам не превышай лимиты.
- Тезис ПРИ ПОКУПКЕ обязателен (что покупаешь, почему, что изменит мнение) -- без него buy_stock откажет.
- Выход из позиции -- целиком твоё суждение. Никакого механического стоп-лосса/тейк-профита за тебя не считает -- решаешь сам, когда тезис исчерпан или опровергнут.
- Без предохранителя на просадку всего портфеля -- если веришь в тезис, не обязан продавать из-за общей просадки.

В начале сессии сначала вызови portfolio_report, чтобы увидеть текущее состояние (позиции, тезисы, кэш, лимиты). Дальше решай: держать/продать существующее, покупать новое, или ничего не делать сегодня -- бездействие тоже нормальный, законный исход. Используй WebSearch для исследования кандидатов при необходимости. В конце -- краткое (3-6 предложений, по-русски) резюме того, что сделал и почему, для владельца портфеля."""


async def run_session(prompt: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"pbumklod": server},
        allowed_tools=[
            "mcp__pbumklod__portfolio_report",
            "mcp__pbumklod__buy_stock",
            "mcp__pbumklod__sell_stock",
            "WebSearch",
        ],
        permission_mode="dontAsk",
        max_turns=20,
    )

    text_chunks = []
    actions = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_chunks.append(block.text)
                    logging.info(f"[TEXT] {block.text[:200]}")
                elif isinstance(block, ToolUseBlock):
                    logging.info(f"[TOOL] {block.name} {block.input}")
                    if block.name in ("mcp__pbumklod__buy_stock", "mcp__pbumklod__sell_stock"):
                        actions.append(f"{block.name.split('__')[-1]}: {block.input}")
        elif isinstance(message, ResultMessage):
            cost = getattr(message, "total_cost_usd", None)
            logging.info(f"[RESULT] cost=${cost}")

    summary = text_chunks[-1] if text_chunks else "(агент не вернул текстовый итог)"
    if actions:
        summary = "Действия:\n" + "\n".join(actions) + "\n\n" + summary
    return summary


async def main():
    from utils import was_us_market_open_yesterday
    if not was_us_market_open_yesterday():
        logging.info("⏭️ Вчера рынок США не торговал -- пропускаю проверку (тот же гейт, что у утреннего дайджеста).")
        return

    now = datetime.now(timezone.utc).replace(microsecond=0)
    prompt = f"Ежедневная проверка ПБумКлод ({now.isoformat()} UTC). Реши, нужны ли покупки или продажи сегодня."
    try:
        summary = await run_session(prompt)
    except Exception as e:
        logging.error(f"Сбой сессии агента: {e}")
        from analytics.price_move_watcher import send_alert_notification
        send_alert_notification(OWNER_TELEGRAM_ID, f"🤖 ПБумКлод: сбой ежедневной проверки -- {e}")
        return
    from analytics.price_move_watcher import send_alert_notification
    send_alert_notification(OWNER_TELEGRAM_ID, f"🤖 ПБумКлод (ежедневная проверка):\n{summary}")
    logging.info("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
