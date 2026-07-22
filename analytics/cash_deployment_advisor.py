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

    # Freedom Broker (short_name='FB') ограничен рынком США: LSE-тикеры (.L) несут
    # системную путаницу пенсов/фунтов в синхронизации фундаментала (current_price и
    # target_mean_price оказываются в разных единицах, ~100x друг от друга) -- сама
    # ФБ путает то же самое в своих прогнозах. Пользователь решил не предлагать
    # лондонские бумаги в портфели ФБ; для Trading 212 (short_name='T212') вопрос
    # пенсы/фунты пока открыт и сознательно отложен.
    US_EXCHANGE_CODES = ("XNYS", "XNAS", "ARCX", "XNMS", "EDGX", "OTCM")

    def __init__(self, db_instance):
        self.db = db_instance
        self.evaluator = TickerEvaluator(db_instance=db_instance)

    def _get_broker_short_name(self, portfolio_id: int) -> str:
        row = self.db.execute_row(f"""
            SELECT b.short_name
            FROM public.portfolios p
            JOIN public.brokers b ON p.broker_id = b.id
            WHERE p.id = {int(portfolio_id)};
        """)
        return (row or {}).get("short_name")

    def _get_strategies_with_keys(self, portfolio_id: int) -> list:
        sql = f"""
            SELECT s.id, s.strategy_name, s.strategy_share_pct, s.rules_config, tpl.system_key
            FROM public.strategies s
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            WHERE s.portfolio_id = {int(portfolio_id)} AND s.is_active = true;
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

    def _get_all_ticker_ids(self, us_only: bool) -> list:
        sql = "SELECT id FROM public.tickers"
        if us_only:
            codes = "', '".join(self.US_EXCHANGE_CODES)
            sql += f" WHERE exchange_mic IN ('{codes}')"
        sql += ";"
        rows = self.db.execute_query(sql) or []
        clean_rows = rows if isinstance(rows, list) else [rows]
        return [int(r["id"]) for r in clean_rows if r]

    def _get_step1_tactic(self, strategy_id: int) -> dict:
        row = self.db.execute_row(
            f"SELECT budget_share_pct, trigger_conditions FROM public.strategy_tactics "
            f"WHERE strategy_id = {int(strategy_id)} AND step_number = 1;"
        )
        return row or {}

    def _find_best_candidate(self, portfolio_id: int, strategy_id: int, held_ids: set, all_ids: list):
        best = None
        best_rank = None
        for tid in all_ids:
            if tid in held_ids:
                continue
            report = self.evaluator.evaluate_ticker_strategy(ticker_id=tid, target_portfolio_id=portfolio_id)
            info = report.get("explain_map", {}).get(strategy_id)
            if not info or not info.get("is_compatible_technically"):
                continue
            rank = float(info.get("ranking_value") or 0.0)
            if best is None or rank > best_rank:
                best = {"ticker_id": tid, "symbol": report.get("symbol"), "ranking_value": rank}
                best_rank = rank
        return best

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
                "pct_underfunded": slack / ideal_budget,
            })

        if not candidates:
            return []

        candidates.sort(key=lambda c: c["pct_underfunded"], reverse=True)

        broker_short_name = self._get_broker_short_name(portfolio_id)
        us_only = (broker_short_name == "FB")

        held_ids = self._get_held_ticker_ids(portfolio_id)
        all_ids = self._get_all_ticker_ids(us_only=us_only)

        recommendations = []
        remaining_pool = deployable_pool

        for cand in candidates:
            if remaining_pool <= 0:
                break

            max_asset_pct = float(cand["rules_config"].get("portfolio_max_asset_pct") or 5.0)
            target_slot = min(cand["slack_usd"], remaining_pool, total_capital * max_asset_pct / 100.0)
            if target_slot <= 0:
                continue

            pct_underfunded_pretty = round(cand["pct_underfunded"] * 100, 1)
            best = self._find_best_candidate(portfolio_id, cand["strategy_id"], held_ids, all_ids)

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
