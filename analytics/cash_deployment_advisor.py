from analytics.portfolio_inspector import PortfolioInspector
from analytics.analytics_utils import TickerEvaluator


class CashDeploymentAdvisor:
    """
    💰 СОВЕТНИК ПО РАЗМЕЩЕНИЮ ИЗБЫТОЧНОГО КЭША UPORT
    Определяет, скопилось ли в портфеле больше реального кэша, чем положено держать
    в резерве, и если да -- подбирает кандидата на покупку и считает первый шаг
    лесенки входа для самой недофинансированной (в % от собственной цели) стратегии.
    Только рекомендации -- ничего не покупает и не переводит само.
    """

    CONTENT_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING")

    def __init__(self, db_instance):
        self.db = db_instance
        self.evaluator = TickerEvaluator(db_instance=db_instance)

    def _get_strategies_with_keys(self, portfolio_id: int) -> list:
        # is_screening_active = false -- пассивная стратегия (заводской набор шаблонов,
        # см. Claude/BACKLOG.md п.9, 2026-07-31): не предлагаем в неё докупку, пока
        # пользователь сам её не включит. is_active отдельно -- жива ли строка вообще.
        sql = f"""
            SELECT s.id, s.strategy_name, s.strategy_share_pct, s.rules_config, tpl.system_key
            FROM public.strategies s
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            WHERE s.portfolio_id = {int(portfolio_id)} AND s.is_active = true AND s.is_screening_active = true;
        """
        rows = self.db.execute_query(sql) or []
        return rows if isinstance(rows, list) else [rows]

    def _get_held_ticker_ids(self, portfolio_id: int) -> set:
        sql = f"""
            SELECT DISTINCT lt.id AS ticker_id
            FROM public.assets a
            JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
            WHERE a.portfolio_id = {int(portfolio_id)} AND a.quantity > 0;
        """
        rows = self.db.execute_query(sql) or []
        clean_rows = rows if isinstance(rows, list) else [rows]
        return {int(r["ticker_id"]) for r in clean_rows if r}

    def _get_step1_tactic(self, strategy_id: int) -> dict:
        row = self.db.execute_row(
            f"SELECT budget_share_pct, trigger_conditions FROM public.strategy_tactics "
            f"WHERE strategy_id = {int(strategy_id)} AND step_number = 1;"
        )
        return row or {}

    def _find_best_candidate(self, strategy_id: int, held_ids: set, us_only: bool):
        # Переиспользует быстрый пакетный скан TickerEvaluator (analytics_utils.py) --
        # тот же источник правды для условий отбора, что и evaluate_ticker_strategy,
        # без N+1 запросов к БД. Результат уже отсортирован по ranking_value.
        results = self.evaluator.screen_universe_for_strategy(
            strategy_id, exclude_ticker_ids=held_ids, us_only=us_only
        )
        return results[0] if results else None

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

        slot_cap = self._compute_slot_cap(rules_config, ideal_budget_usd, total_capital)

        tactic = self._get_step1_tactic(strategy_id)
        step1_share_pct = float(tactic.get("budget_share_pct") or 100.0)
        return round(slot_cap * step1_share_pct / 100.0, 2)

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

        recommendations = []
        remaining_pool = deployable_pool

        for cand in candidates:
            if remaining_pool <= 0:
                break

            slot_cap = self._compute_slot_cap(cand["rules_config"], cand["ideal_budget_usd"], total_capital)
            target_slot = min(cand["slack_usd"], remaining_pool, slot_cap)
            if target_slot <= 0:
                continue

            pct_underfunded_pretty = round(cand["pct_underfunded"] * 100, 1)
            best = self._find_best_candidate(cand["strategy_id"], held_ids, us_only)

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
                        f"(${target_slot:,.2f} доступно), но ни один тикер не прошёл экран стратегии."
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
                    f"Стратегия недофинансирована на {pct_underfunded_pretty}% от цели. "
                    f"Кандидат {best['symbol']} прошёл экран стратегии (ranking={best['ranking_value']}). "
                    f"Переведи ${step1_amount:,.2f} на торговый счёт и купи {best['symbol']} по рынку "
                    f"(шаг 1 лесенки, {step1_share_pct:.0f}% от расчётного слота ${target_slot:,.2f})."
                ),
            })

            remaining_pool -= target_slot

        return recommendations
