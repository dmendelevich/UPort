from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import TickerEvaluator, check_sector_ceiling_breach


class CashDeploymentAdvisor:
    """
    💰 СОВЕТНИК ПО РАЗМЕЩЕНИЮ ИЗБЫТОЧНОГО КЭША UPORT
    Определяет, скопилось ли в портфеле больше реального кэша, чем положено держать
    в резерве, и если да -- подбирает кандидата на покупку и считает первый шаг
    лесенки входа для самой недофинансированной (в % от собственной цели) стратегии.
    Только рекомендации -- ничего не покупает и не переводит само.
    """

    CONTENT_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING", "INDEX_CORE")

    def __init__(self, db_instance):
        self.db = db_instance
        self.evaluator = TickerEvaluator(db_instance=db_instance)

    def _get_strategies_with_keys(self, portfolio_id: int) -> list:
        # is_screening_active = false -- пассивная стратегия (заводской набор шаблонов,
        # см. Claude/BACKLOG.md п.9, 2026-07-31): не предлагаем в неё докупку, пока
        # пользователь сам её не включит. is_active отдельно -- жива ли строка вообще.
        sql = """
            SELECT s.id, s.strategy_name, s.strategy_share_pct, s.rules_config, tpl.system_key
            FROM public.strategies s
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            WHERE s.portfolio_id = %s AND s.is_active = true AND s.is_screening_active = true;
        """
        rows = self.db.execute_query(sql, (portfolio_id,)) or []
        return rows if isinstance(rows, list) else [rows]

    def _get_held_ticker_ids(self, portfolio_id: int) -> set:
        sql = """
            SELECT DISTINCT lt.id AS ticker_id
            FROM public.assets a
            JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
            WHERE a.portfolio_id = %s AND a.quantity > 0;
        """
        rows = self.db.execute_query(sql, (portfolio_id,)) or []
        clean_rows = rows if isinstance(rows, list) else [rows]
        return {int(r["ticker_id"]) for r in clean_rows if r}

    def _get_step1_tactic(self, strategy_id: int) -> dict:
        row = self.db.execute_row(
            "SELECT budget_share_pct, trigger_conditions FROM public.strategy_tactics "
            "WHERE strategy_id = %s AND step_number = 1;",
            (strategy_id,)
        )
        return row or {}

    def _find_candidates_for_strategy(self, strategy_id: int, held_ids: set, us_only: bool,
                                       sector_exposure: dict, sector_target_config: dict,
                                       total_capital: float, available_usd: float, slot_cap: float):
        """
        Заполняет столько полноразмерных слотов, сколько влезает по РЕАЛЬНЫМ деньгам
        (available_usd), не искусственный предел "один победитель за прогон"
        (Claude/BACKLOG.md №163, живой разбор с пользователем 2026-09-02: AAPL/JNJ/XOM
        честно проходили вход Трендовой неделями, просто не выбирались, потому что
        кто-то один ранжировался выше в конкретный день проверки, а система не умела
        отдать больше одного). Переиспользует быстрый пакетный скан TickerEvaluator --
        тот же источник правды, что и evaluate_ticker_strategy, результат уже
        отсортирован по ranking_value.

        Секторальный потолок теперь ЖЁСТКИЙ для ВСЕХ содержательных стратегий (раньше
        был только у Консервативной, Р/Т получали лишь текстовое предупреждение,
        BACKLOG.md №82) -- при нескольких покупках за один прогон риск ссыпать всё в
        один сектор выше, чем при одной покупке в день, решено явно не оставлять
        мягким.

        sector_exposure МУТИРУЕТСЯ по ходу (общий портфельный рентген по ВСЕМ
        стратегиям, не по одной) -- каждая успешная покупка добавляется в бегущий
        итог ДО проверки следующего кандидата, в том числе кандидата ДРУГОЙ стратегии
        в этом же вызове evaluate_deployment (тот же словарь передаётся сквозь весь
        цикл там) -- иначе три покупки в один сектор за один прогон каждая по
        отдельности прошла бы проверку "не пробивает потолок", хотя вместе пробивают.

        Возвращает список [(candidate, target_slot_usd), ...] -- пусто, если ничего
        не прошло экран или не осталось денег/сектор везде закрыт.
        """
        results = self.evaluator.screen_universe_for_strategy(
            strategy_id, exclude_ticker_ids=held_ids, us_only=us_only
        )
        picks = []
        remaining = available_usd
        local_held = set(held_ids)

        for cand in results:
            if remaining <= 0:
                break
            if cand["ticker_id"] in local_held:
                continue
            target_slot = min(slot_cap, remaining)
            if target_slot <= 0:
                break
            breach = check_sector_ceiling_breach(
                sector_exposure, total_capital, sector_target_config,
                cand.get("sector"), additional_usd=target_slot,
            )
            if breach:
                continue
            picks.append((cand, target_slot))
            remaining -= target_slot
            local_held.add(cand["ticker_id"])
            sector = cand.get("sector") or "Unknown Sector"
            sector_exposure[sector] = sector_exposure.get(sector, 0.0) + target_slot

        return picks

    def _get_index_core_leg_values(self, strategy_id: int, symbols: list) -> dict:
        """$-стоимость каждой ноги Индексного ядра (VTI/VXUS/BND), уже держимой этой стратегией."""
        sql = """
            SELECT t.symbol, sa.allocated_quantity * l.last_price AS usd
            FROM public.strategy_assets sa
            JOIN public.assets a ON a.id = sa.asset_id
            JOIN public.listings l ON l.id = a.listing_id
            JOIN public.tickers t ON t.id = l.ticker_id
            WHERE sa.strategy_id = %s AND t.symbol = ANY(%s) AND sa.allocated_quantity > 0;
        """
        rows = self.db.execute_query(sql, (strategy_id, symbols)) or []
        clean_rows = rows if isinstance(rows, list) else [rows]
        return {r["symbol"]: float(r["usd"] or 0.0) for r in clean_rows if r}

    def _find_index_core_leg(self, strategy_id: int, rules_config: dict, ideal_budget_usd: float) -> dict:
        """
        Индексное ядро (Claude/15_index_core.md) -- не конкурс кандидатов через экран,
        а выбор ОДНОЙ из фиксированных курируемых ног (VTI/VXUS/BND, `rules_config
        ["index_core_target_weights"]`), дальше всего отставшей от своего целевого
        веса от ЦЕЛЕВОГО бюджета всей стратегии. Никакого ранжирования/скрининга --
        сектор сознательно не проверяется (широкие индексные фонды пока не участвуют
        в секторальном аудите, см. Claude/16_selection_logic_audit.md, находка G).
        Возвращает {"ticker_id", "symbol", "ranking_value": None} или None.
        """
        weights = rules_config.get("index_core_target_weights") or {}
        if not weights:
            return None
        symbols = list(weights.keys())
        held_values = self._get_index_core_leg_values(strategy_id, symbols)

        best_symbol, best_deficit = None, 0.0
        for symbol, weight_pct in weights.items():
            target_usd = ideal_budget_usd * float(weight_pct) / 100.0
            current_usd = held_values.get(symbol, 0.0)
            deficit = target_usd - current_usd
            if deficit > best_deficit:
                best_deficit = deficit
                best_symbol = symbol
        if not best_symbol:
            return None

        ticker_row = self.db.execute_row("SELECT id FROM public.tickers WHERE symbol = %s;", (best_symbol,))
        if not ticker_row:
            return None
        return {"ticker_id": int(ticker_row["id"]), "symbol": best_symbol, "ranking_value": None}

    def _compute_slot_cap(self, rules_config: dict, ideal_budget_usd: float, total_capital: float) -> float:
        """
        Целевой размер слота (ДО применения share_pct шага 1) -- приоритет сверху вниз
        (Claude/13_portfolio_construction_and_rebalancing_rules.md, 2026-08-03):
        1) tactic_slot_fixed_usd -- фиксированная сумма (Револьверная $1000, Трендовая
           $3000): число одновременно доступных качественных идей не растёт вместе с
           капиталом семьи, поэтому размер слота ФИКСИРОВАН, а растёт число слотов.
        2) tactic_slot_pct_of_strategy -- % от ЦЕЛЕВОГО бюджета САМОЙ стратегии.
        3) иначе -- потолок от ОБЩЕГО капитала портфеля (portfolio_max_asset_pct).

        portfolio_max_asset_pct -- ВСЕГДА жёсткий потолок сверху, независимо от того,
        откуда взялась исходная сумма (BACKLOG.md №74, живой случай 2026-08-05: у П10,
        капитал $10 439, фиксированный слот Трендовой $3000 оказался 28.7% капитала при
        лимите на бумагу 3% -- формула это раньше не проверяла вообще, риск-лимит можно
        было пробить собственной рекомендацией системы).
        """
        slot_fixed_usd = rules_config.get("tactic_slot_fixed_usd")
        slot_pct_of_strategy = rules_config.get("tactic_slot_pct_of_strategy")
        if slot_fixed_usd is not None:
            slot_cap = float(slot_fixed_usd)
        elif slot_pct_of_strategy is not None:
            slot_cap = ideal_budget_usd * float(slot_pct_of_strategy) / 100.0
        else:
            slot_cap = None

        max_asset_pct = float(rules_config.get("portfolio_max_asset_pct") or 5.0)
        hard_cap = total_capital * max_asset_pct / 100.0
        return hard_cap if slot_cap is None else min(slot_cap, hard_cap)

    def compute_slot_size(self, portfolio_id: int, strategy_id: int) -> float:
        """
        Целевой размер слота (шаг 1 лесенки) для стратегии -- та же формула, что и
        в evaluate_deployment (см. _compute_slot_cap), но БЕЗ проверки, есть ли реально
        недофинансирование (slack). Нужно для "Всё равно купить" -- ручного
        форс-оверрайда совета системы (BACKLOG.md №73) -- там условия "стратегия
        недофинансирована" может не быть вовсе (пользователь хочет купить, даже если
        стратегия уже на цели).
        """
        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()
        total_capital = float(balances.get("total_capital_usd") or 0.0)
        if total_capital <= 0:
            return 0.0

        strat_rows = self._get_strategies_with_keys(portfolio_id)
        strat_row = next((r for r in strat_rows if int(r["id"]) == int(strategy_id)), None)
        if not strat_row:
            return 0.0
        rules_config = strat_row.get("rules_config") or {}

        bal = next(
            (b for s_id, b in balances.get("strategies", {}).items() if int(s_id) == int(strategy_id)),
            {}
        )
        ideal_budget_usd = float((bal or {}).get("ideal_budget_usd") or 0.0)

        if strat_row.get("system_key") == "INDEX_CORE":
            # Ядро не пользуется лесенкой/слотом -- весь доступный слэк стратегии разом
            # (Claude/15_index_core.md), та же логика, что и ветка INDEX_CORE в
            # evaluate_deployment ниже. _compute_slot_cap здесь неприменима (её потолок
            # portfolio_max_asset_pct рассчитан на ОДНУ бумагу, не на курируемую тройку
            # с фиксированными весами) -- используя её, process_paper_buy_yes считал бы
            # неверную (заниженную) сумму сделки для ядра.
            slack = float((bal or {}).get("virtual_free_cash_usd") or 0.0)
            return round(max(slack, 0.0), 2)

        slot_cap = self._compute_slot_cap(rules_config, ideal_budget_usd, total_capital)

        tactic = self._get_step1_tactic(strategy_id)
        step1_share_pct = float(tactic.get("budget_share_pct") or 100.0)
        return round(slot_cap * step1_share_pct / 100.0, 2)

    def get_index_core_leg_status(self, portfolio_id: int, strategy_id: int) -> list:
        """
        Побуждение 2 для Индексного ядра (Claude/BACKLOG.md №122, шаг 6) -- у ядра нет
        конкурса кандидатов через экран, «Предложения» должны показывать не топ-10, а
        честное состояние всех курируемых ног (VTI/VXUS/BND): держим/цель/% от цели.
        Тот же источник данных, что _find_index_core_leg использует для выбора ОДНОЙ
        самой отставшей ноги -- здесь просто не останавливаемся на первой, отдаём все.
        Возвращает [] для не-INDEX_CORE стратегии или стратегии без rules_config.

        Каждая нога кликабельна через ту же "🤝 К сделке" (bot_handlers/deal.py) --
        verify_buy_candidate/compute_slot_size уже умеют работать с ЛЮБОЙ ногой ядра,
        не только с автоматически выбранной самой отставшей (см. их же INDEX_CORE-ветки).
        """
        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()

        strat_rows = self._get_strategies_with_keys(portfolio_id)
        strat_row = next((r for r in strat_rows if int(r["id"]) == int(strategy_id)), None)
        if not strat_row or strat_row.get("system_key") != "INDEX_CORE":
            return []

        rules_config = strat_row.get("rules_config") or {}
        weights = rules_config.get("index_core_target_weights") or {}
        if not weights:
            return []

        bal = next(
            (b for s_id, b in balances.get("strategies", {}).items() if int(s_id) == int(strategy_id)),
            {}
        )
        ideal_budget_usd = float((bal or {}).get("ideal_budget_usd") or 0.0)

        symbols = list(weights.keys())
        held_values = self._get_index_core_leg_values(strategy_id, symbols)

        ticker_rows = self.db.execute_query("SELECT id, symbol FROM public.tickers WHERE symbol = ANY(%s);", (symbols,)) or []
        ticker_rows = ticker_rows if isinstance(ticker_rows, list) else [ticker_rows]
        ticker_id_by_symbol = {r["symbol"]: int(r["id"]) for r in ticker_rows if r}

        legs = []
        for symbol, weight_pct in weights.items():
            target_usd = ideal_budget_usd * float(weight_pct) / 100.0
            held_usd = held_values.get(symbol, 0.0)
            legs.append({
                "symbol": symbol,
                "ticker_id": ticker_id_by_symbol.get(symbol),
                "held_usd": round(held_usd, 2),
                "target_usd": round(target_usd, 2),
                "pct_of_target": round(held_usd / target_usd * 100, 1) if target_usd > 0 else None,
            })
        legs.sort(key=lambda leg: (leg["pct_of_target"] if leg["pct_of_target"] is not None else 0))
        return legs

    def verify_buy_candidate(self, portfolio_id: int, strategy_id: int, ticker_id: int) -> dict:
        """
        Проверка "всё ещё можно ли купить именно этот тикер под эту стратегию" --
        менее строгая версия evaluate_deployment (та признаёт СТРОГО ОДНОГО лучшего
        по рангу кандидата на стратегию). Нужна для paper_buy_yes (BACKLOG.md №74,
        живой кейс NRG, 2026-08-05): «Предложения» показывают топ-10 кандидатов для
        обзора, а evaluate_deployment -- только текущего лидера рейтинга; клик на
        любого другого из топ-10 раньше ВСЕГДА проваливался бы как "условия
        изменились", даже если тикер честно проходит экран стратегии и деньги под
        неё ещё есть -- просто он не сегодняшний фаворит по ранжированию.

        Проверяет: тикер ещё не куплен в портфеле, стратегия содержательная и
        активна, у стратегии есть свободный кэш (slack), тикер проходит экран
        стратегии ПРЯМО СЕЙЧАС. Возвращает {} если что-то не так, иначе
        {"symbol":..., "strategy_name":...}.
        """
        held_ids = self._get_held_ticker_ids(portfolio_id)
        if int(ticker_id) in held_ids:
            return {}

        strat_rows = self._get_strategies_with_keys(portfolio_id)
        strat_row = next((r for r in strat_rows if int(r["id"]) == int(strategy_id)), None)
        if not strat_row or strat_row.get("system_key") not in self.CONTENT_SYSTEM_KEYS:
            return {}

        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()
        bal = next(
            (b for s_id, b in balances.get("strategies", {}).items() if int(s_id) == int(strategy_id)),
            {}
        )
        slack = float((bal or {}).get("virtual_free_cash_usd") or 0.0)
        if slack <= 0:
            return {}

        if strat_row.get("system_key") == "INDEX_CORE":
            # У ядра нет экрана совместимости (evaluate_ticker_strategy её не знает) --
            # вместо этого проверяем, что тикер вообще одна из курируемых ног ядра И у
            # неё всё ещё положительный дефицит от целевого веса (Claude/15_index_core.md).
            ideal_budget_usd = float((bal or {}).get("ideal_budget_usd") or 0.0)
            rules_config = strat_row.get("rules_config") or {}
            weights = rules_config.get("index_core_target_weights") or {}
            symbol_row = self.db.execute_row("SELECT symbol FROM public.tickers WHERE id = %s;", (ticker_id,))
            symbol = (symbol_row or {}).get("symbol")
            if not symbol or symbol not in weights:
                return {}
            held_values = self._get_index_core_leg_values(strategy_id, [symbol])
            target_usd = ideal_budget_usd * float(weights[symbol]) / 100.0
            if target_usd - held_values.get(symbol, 0.0) <= 0:
                return {}
            return {"symbol": symbol, "strategy_name": strat_row.get("strategy_name")}

        report = self.evaluator.evaluate_ticker_strategy(ticker_id, portfolio_id)
        info = (report.get("explain_map") or {}).get(int(strategy_id))
        if not info or not info.get("is_compatible_technically"):
            return {}

        return {"symbol": report.get("symbol"), "strategy_name": strat_row.get("strategy_name")}

    def evaluate_deployment(self, portfolio_id: int) -> list:
        """
        Главная точка входа. Возвращает список рекомендаций по размещению
        избыточного кэша (обычно 0 или 1 запись, но структурно готова к нескольким,
        если однажды несколько стратегий одновременно окажутся недофинансированы).
        """
        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()
        total_capital = float(balances.get("total_capital_usd") or 0.0)
        if total_capital <= 0:
            return []

        strat_rows = self._get_strategies_with_keys(portfolio_id)
        strat_by_id = {int(r["id"]): r for r in strat_rows if r}

        cash_reserve_row = next((r for r in strat_rows if r.get("system_key") == "CASH_RESERVE"), None)
        cash_reserve_config = (cash_reserve_row or {}).get("rules_config") or {}
        reserve_floor = float(cash_reserve_config.get("cash_min_untouchable_usd") or 0.0)
        min_threshold = float(cash_reserve_config.get("cash_auto_invest_threshold_usd") or 0.0)

        # Сумма virtual_free_cash_usd по ВСЕМ стратегиям портфеля алгебраически равна
        # реальному кэшу на счетах (доли стратегий в сумме дают 100% капитала) --
        # поэтому отдельный запрос к accounts не нужен.
        real_total_cash = sum(
            float(v.get("virtual_free_cash_usd") or 0.0)
            for v in balances.get("strategies", {}).values()
        )
        deployable_pool = real_total_cash - reserve_floor

        if deployable_pool < min_threshold:
            return []

        # Кандидаты -- содержательные стратегии с положительным slack, отсортированные
        # по проценту недофинансированности СОБСТВЕННОЙ цели, а не по абсолютному $
        # (крупная по доле стратегия иначе почти всегда выигрывала бы у мелкой).
        candidates = []
        for s_id, bal in balances.get("strategies", {}).items():
            strat_row = strat_by_id.get(int(s_id))
            if not strat_row or strat_row.get("system_key") not in self.CONTENT_SYSTEM_KEYS:
                continue
            slack = float(bal.get("virtual_free_cash_usd") or 0.0)
            ideal_budget = float(bal.get("ideal_budget_usd") or 0.0)
            if slack <= 0 or ideal_budget <= 0:
                continue
            candidates.append({
                "strategy_id": int(s_id),
                "strategy_name": strat_row.get("strategy_name"),
                "system_key": strat_row.get("system_key"),
                "rules_config": strat_row.get("rules_config") or {},
                "slack_usd": slack,
                "ideal_budget_usd": ideal_budget,
                "pct_underfunded": slack / ideal_budget,
            })

        if not candidates:
            return []

        candidates.sort(key=lambda c: c["pct_underfunded"], reverse=True)

        us_only = self.evaluator.resolve_us_only(portfolio_id)

        held_ids = self._get_held_ticker_ids(portfolio_id)

        # Секторальный рентген по факту -- считаем ОДИН раз на весь вызов (не на
        # каждого кандидата), это факт текущего портфеля, а не гипотеза, которая
        # менялась бы от рекомендации к рекомендации внутри одного и того же прогона
        # (Claude/BACKLOG.md №82).
        sector_exposure = inspector.get_portfolio_sector_exposure()
        sector_target_config = inspector.sector_target_config

        recommendations = []
        remaining_pool = deployable_pool

        for cand in candidates:
            if remaining_pool <= 0:
                break

            is_index_core = cand["system_key"] == "INDEX_CORE"
            pct_underfunded_pretty = round(cand["pct_underfunded"] * 100, 1)

            if is_index_core:
                # Индексное ядро не пользуется лесенкой/слотом -- весь доступный слэк
                # разом, целиком в самую недовешенную ногу (Claude/15_index_core.md).
                # Ниже порога сделки просто ждём, копим слэк дальше -- не отдельная
                # рекомендация "недостаточно денег" на каждый прогон. У ядра по
                # определению одна нога за раз (не конкурс кандидатов через экран) --
                # многослотовое заполнение (BACKLOG.md №163) ядра не касается.
                min_trade = float(cand["rules_config"].get("index_core_min_trade_usd") or 300.0)
                target_slot = min(cand["slack_usd"], remaining_pool)
                if target_slot < min_trade:
                    continue
                best = self._find_index_core_leg(cand["strategy_id"], cand["rules_config"], cand["ideal_budget_usd"])
                if not best:
                    recommendations.append({
                        "portfolio_id": portfolio_id,
                        "strategy_id": cand["strategy_id"],
                        "strategy_name": cand["strategy_name"],
                        "system_key": cand["system_key"],
                        "status": "NO_CANDIDATE",
                        "pct_underfunded": pct_underfunded_pretty,
                        "deployable_usd": round(target_slot, 2),
                        "reason": (
                            f"Недофинансирована на {pct_underfunded_pretty}% от цели "
                            f"(${target_slot:,.2f} доступно), но целевые веса ядра не заданы (rules_config)."
                        ),
                    })
                    continue

                tactic = self._get_step1_tactic(cand["strategy_id"])
                step1_share_pct = float(tactic.get("budget_share_pct") or 100.0)
                step1_amount = target_slot * step1_share_pct / 100.0
                mode = (tactic.get("trigger_conditions") or {}).get("mode", "market")
                recommendations.append({
                    "portfolio_id": portfolio_id,
                    "strategy_id": cand["strategy_id"],
                    "strategy_name": cand["strategy_name"],
                    "system_key": cand["system_key"],
                    "status": "CANDIDATE_FOUND",
                    "pct_underfunded": pct_underfunded_pretty,
                    "ticker_id": best["ticker_id"],
                    "symbol": best["symbol"],
                    "target_slot_usd": round(target_slot, 2),
                    "step1_amount_usd": round(step1_amount, 2),
                    "step1_mode": mode,
                    "ranking_value": best["ranking_value"],
                    "reason": (
                        f"Индексное ядро недофинансировано на {pct_underfunded_pretty}% от цели. "
                        f"{best['symbol']} дальше всего отстал от своего целевого веса. "
                        f"Переведи ${step1_amount:,.2f} на торговый счёт и купи {best['symbol']} по рынку."
                    ),
                })
                remaining_pool -= target_slot
                continue

            # Р/К/Т -- заполняем столько полноразмерных слотов, сколько влезает по
            # реальным деньгам (BACKLOG.md №163), не искусственно один за прогон.
            slot_cap = self._compute_slot_cap(cand["rules_config"], cand["ideal_budget_usd"], total_capital)
            available_usd = min(cand["slack_usd"], remaining_pool)
            if available_usd <= 0:
                continue

            picks = self._find_candidates_for_strategy(
                cand["strategy_id"], held_ids, us_only,
                sector_exposure, sector_target_config, total_capital, available_usd, slot_cap,
            )

            if not picks:
                recommendations.append({
                    "portfolio_id": portfolio_id,
                    "strategy_id": cand["strategy_id"],
                    "strategy_name": cand["strategy_name"],
                    "system_key": cand["system_key"],
                    "status": "NO_CANDIDATE",
                    "pct_underfunded": pct_underfunded_pretty,
                    "deployable_usd": round(available_usd, 2),
                    "reason": (
                        f"Недофинансирована на {pct_underfunded_pretty}% от цели "
                        f"(${available_usd:,.2f} доступно), но ни один тикер не прошёл экран стратегии "
                        f"(с учётом секторальных лимитов)."
                    ),
                })
                continue

            tactic = self._get_step1_tactic(cand["strategy_id"])
            step1_share_pct = float(tactic.get("budget_share_pct") or 100.0)
            mode = (tactic.get("trigger_conditions") or {}).get("mode", "market")

            for best, target_slot in picks:
                step1_amount = target_slot * step1_share_pct / 100.0
                recommendations.append({
                    "portfolio_id": portfolio_id,
                    "strategy_id": cand["strategy_id"],
                    "strategy_name": cand["strategy_name"],
                    "system_key": cand["system_key"],
                    "status": "CANDIDATE_FOUND",
                    "pct_underfunded": pct_underfunded_pretty,
                    "ticker_id": best["ticker_id"],
                    "symbol": best["symbol"],
                    "target_slot_usd": round(target_slot, 2),
                    "step1_amount_usd": round(step1_amount, 2),
                    "step1_mode": mode,
                    "ranking_value": best["ranking_value"],
                    "reason": (
                        f"Стратегия недофинансирована на {pct_underfunded_pretty}% от цели. "
                        f"Кандидат {best['symbol']} прошёл экран стратегии (ranking={best['ranking_value']}). "
                        f"Переведи ${step1_amount:,.2f} на торговый счёт и купи {best['symbol']} по рынку "
                        f"(шаг 1 лесенки, {step1_share_pct:.0f}% от расчётного слота ${target_slot:,.2f})."
                    ),
                })
                remaining_pool -= target_slot
                held_ids.add(best["ticker_id"])

        return recommendations
