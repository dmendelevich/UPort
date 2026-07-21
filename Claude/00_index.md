# UPort — заметки по архитектуре (Claude)

Эта папка — рабочий блокнот, не часть кода системы. Здесь фиксируются:
- архитектура и принципы работы отдельных модулей,
- решения и их обоснования (почему сделано именно так),
- договорённости/термины, введённые пользователем в разговоре,
- открытые вопросы и TODO.

Заполняется по ходу разбора системы с пользователем (dmend), начиная с 2026-07-20.

## Файлы

(будет пополняться по мере разбора)

## Общая структура репозитория (первичный обзор, требует уточнения)

- `main.py` — точка входа
- `config.py`, `settings.py` — конфигурация
- `database.py` — работа с БД (44K, крупный модуль)
- `cron_scheduler.py` — планировщик задач
- `bot_handlers/` — обработчики Telegram-бота (backlog, portfolios, settings, strategy_resolver, summary, ticker_search, tickers, watchlist, bot_keyboards, bot_screens, bot_utils, common)
- `brokers_connectors/` — коннекторы к брокерам (fb_client, fb_websocket_daemon, sync_account_fb, sync_alerts_fb, sync_quotes_fb, sync_quotes_t212, sync_strategy_asset_fb) — похоже на Freedom Broker (fb) и Trading212 (t212)
- `site_connectors/` — внешние источники данных (sync_rates_yhoo, sync_fundamentals_yhoo, sync_signals_yf — Yahoo Finance; market_scanner, vsegpt_client, trigger_etf_look_through, mass_check_passports)
- `analytics/` — аналитика портфеля (analytics_utils, portfolio_auditor, portfolio_inspector)
- `exchanges_info/` — загрузка справочников бирж
- `ARCHITECTURE/` — уже существующая документация автора (ARCHITECTURE_tickers.md, Time.md, anal_core.md, tg_bot.md, scaners_V01)
- `uport_ai_bot.py`, `uport_ai_gateway.py` — AI-интеграция (возможно, vsegpt/GPT-шлюз)
- тесты в корне (`test_*.py`) — pytest-подобные, без папки tests/

Требует подтверждения и уточнения от пользователя по ходу разбора.
