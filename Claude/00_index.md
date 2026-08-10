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
- [14_paper_portfolio.md](14_paper_portfolio.md) — бумажный (виртуальный) портфель внутри UPort для проверки системы вперёд по времени + задел на будущее реальное исполнение через подтверждение Да/Нет (`portfolios.execution_mode`); принципы согласованы, план реализации по фазам, код не начат
- [15_index_core.md](15_index_core.md) — «Индексное ядро», четвёртая содержательная стратегия (VTI+VXUS+BND, делит долю Консервативной, не добавляется сверху); принципы полностью согласованы, код не начат
- [16_selection_logic_audit.md](16_selection_logic_audit.md) — «Ревизия инвестиционной логики отбора»: инвентарь находок от первого взгляда на реальные результаты отборщиков глазами инвестора (не только код), разного статуса готовности
- [17_strategy_parameter_adaptation_plan.md](17_strategy_parameter_adaptation_plan.md) — точка входа для ПРОДОЛЖЕНИЯ темы №16 в новом диалоге: что готово к реализации сразу (буфер дисконта K=2.5, FCF NULL-баг, target_mean_price NULL-баг), что требует обсуждения принципов перед кодом
- [18_db_backup_strategy_prep.md](18_db_backup_strategy_prep.md) — бэкап БД реализован и протестирован (2026-08-09, см. `BACKLOG.md` №105): суточный `pg_dump` + внеочередной после схемных тем, вторая копия на ноутбуке через pull-`rsync`, мониторинг сбоя независимо от БД
- [19_price_move_protection_design.md](19_price_move_protection_design.md) — «спасай деньги» (продолжение №1/№3/№65/№87): три разных сигнала на резкое движение цены — момент исполнения сделки (A), защита капитала от стратегии (B, стоп-лосс/трейлинг), обвал рынка/сектора через VTI/SPDR (C); принципы частично согласованы, код не начат

## Общая структура репозитория (первичный обзор, требует уточнения)

- `main.py` — точка входа
- `config.py`, `settings.py` — конфигурация
- `database.py` — работа с БД (44K, крупный модуль)
- `cron_scheduler.py` — планировщик задач
- `bot_handlers/` — обработчики Telegram-бота (backlog, portfolios, settings, strategy_resolver, summary, ticker_search, tickers, watchlist, bot_keyboards, bot_screens, bot_utils, common)
- `brokers_connectors/` — коннекторы к брокерам (fb_client, fb_websocket_daemon, sync_account_fb, sync_alerts_fb, sync_quotes_fb, sync_quotes_t212, sync_strategy_asset_fb) — похоже на Freedom Broker (fb) и Trading212 (t212)
- `site_connectors/` — внешние источники данных (sync_rates_yhoo, sync_fundamentals_yhoo, sync_signals_yf — Yahoo Finance; market_scanner, trigger_etf_look_through, mass_check_passports)
- `analytics/` — аналитика портфеля (analytics_utils, portfolio_auditor, portfolio_inspector)
- `exchanges_info/` — загрузка справочников бирж
- `ARCHITECTURE/` — уже существующая документация автора (ARCHITECTURE_tickers.md, Time.md, anal_core.md, tg_bot.md, scaners_V01)
- `uport_ai_bot.py` — главный живой Telegram-бот (не про ИИ, несмотря на имя файла — это исторический артефакт); `uport_ai_gateway.py` — HTTP-шлюз к БД (`uport_gateway.service`, порт 3000), через него идёт вообще весь доступ к БД (`db_sys`/`db_bot`), тоже не про ИИ. **Исправлено 2026-08-09**: раньше здесь стояло "возможно, vsegpt/GPT-шлюз" — неверная догадка первого дня; реальная интеграция с VseGPT была отдельным, уже удалённым кодом (`site_connectors/vsegpt_client.py` + `analytics/old_stuff/ai_strategist.py`, см. `Claude/BACKLOG.md`).
- тесты в корне (`test_*.py`) — pytest-подобные, без папки tests/

Требует подтверждения и уточнения от пользователя по ходу разбора.
