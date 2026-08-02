# UPort — заметки по архитектуре (Claude)

Эта папка — рабочий блокнот, не часть кода системы. Здесь фиксируются:
- архитектура и принципы работы отдельных модулей,
- решения и их обоснования (почему сделано именно так),
- договорённости/термины, введённые пользователем в разговоре,
- открытые вопросы и TODO.

Заполняется по ходу разбора системы с пользователем (dmend), начиная с 2026-07-20.

## Файлы

- [01_workflow_rules.md](01_workflow_rules.md) — правила взаимодействия
- [02_universal_views.md](02_universal_views.md) — универсальные view БД
- [03_strategies_and_templates.md](03_strategies_and_templates.md) — стратегии как субпортфели
- [04_gateway_and_portfolio_admin.md](04_gateway_and_portfolio_admin.md) — шлюз и админ портфелей
- [05_strategy_screen_and_kubiki.md](05_strategy_screen_and_kubiki.md) — экран стратегии и общие кубики UI
- [06_tracks.md](06_tracks.md) — три трека работы (Советник / Админ-интерфейс стратегий / Наведение порядка) и принцип "система только советует"
- [07_glossary.md](07_glossary.md) — толковый словарь терминов, полей и параметров системы
- [BACKLOG.md](BACKLOG.md) — бэклог, разложен по трекам
- [08_v1_plan.md](08_v1_plan.md) — направления и приоритеты V1, треки D (email) и E (безопасность)
- [09_pipeline_reconciliation.md](09_pipeline_reconciliation.md) — сверка сделок с order_pipelines: жизненный цикл идеи, лимитные шаги, перенос между стратегиями
- [10_portfolio_strategy_cards_prep.md](10_portfolio_strategy_cards_prep.md) — конспект для следующей сессии: стандартизация карточек портфеля и стратегии (правила, стандарты, процедура, наработанная на теме тикера)
- [11_asset_lifecycle_and_plan.md](11_asset_lifecycle_and_plan.md) — концепция «Плана»: жизненный цикл актива (рождение/жизнь/смерть/реинкарнация), СН как кастрюлька идей, четыре ритма проверки, принцип разделения решения и исполнения
- [12_investment_goal_and_mechanisms_roadmap.md](12_investment_goal_and_mechanisms_roadmap.md) — цель инвестирования (ядро+сателлит, ~15% годовых), сверка архитектуры портфелей с целью, дорожная карта шести недостающих механизмов и согласованный порядок работы
- [13_portfolio_construction_and_rebalancing_rules.md](13_portfolio_construction_and_rebalancing_rules.md) — правила размера слота по стратегиям (относительное для Консервативной, фиксированное для Р/Т), роль `portfolio_max_asset_pct`, ежемесячный ритм ребалансировки (между стратегиями / внутри Консервативной / пересмотр фиксированных сумм); принципы согласованы, реализация не начата

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
