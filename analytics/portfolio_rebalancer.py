from analytics.analytics_utils import convert_to_base_currency
from analytics.portfolio_inspector import PortfolioInspector


class PortfolioRebalancer:
    """
    ⚖️ ПОДРЕЗКА ПЕРЕВЕШЕННЫХ ПОЗИЦИЙ (см. Claude/13_portfolio_construction_and_rebalancing_rules.md)
    Сегодня -- только Консервативная. Слои "между стратегиями" и "внутри стратегии по
    весу бумаги" схлопнулись в один механизм (2026-08-03): Трендовая/Револьверная
    корректируют переинвестированность ПАССИВНО (CashDeploymentAdvisor их просто не
    докармливает, см. evaluate_deployment) -- активно продавать здоровую позицию ради
    структуры портфеля им предлагать нельзя, противоречит "жёсткий стоп"/"без
    усреднений вниз" (BACKLOG.md «Сделано» п.33). Консервативной это не касается --
    там нет "тезиса момента", который можно сломать раньше времени.

    Триггер: позиция выросла больше OVERWEIGHT_MULTIPLIER × плановый слот
    (tactic_slot_pct_of_strategy % от целевого бюджета стратегии) -- подрезать до
    планового слота, не до нуля (не полный выход, тем и отличается от
    PositionExitEvaluator). Закрывает исторический разрыв (BACKLOG.md п.4, GLDM-пример
    2026-07-23): позиция может быть переинвестирована и при этом не давать сигнала на
    продажу от PositionExitEvaluator, потому что критерий выхода стратегии на неё
    структурно неприменим (ETF без ROE/долга).
    """

    OVERWEIGHT_MULTIPLIER = 2.0  # во сколько раз больше планового слота считать перекосом

    def __init__(self, db_instance):
        self.db = db_instance

    def evaluate_portfolio(self, portfolio_id: int) -> list:
        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()

        strat_rows = self.db.execute_query(f"""
            SELECT s.id, s.strategy_name, s.rules_config
            FROM public.strategies s
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            WHERE s.portfolio_id = {int(portfolio_id)} AND tpl.system_key = 'CONSERVATIVE_ACCUMULATION'
              AND s.is_active = true;
        """)
        strat_rows = strat_rows if isinstance(strat_rows, list) else ([strat_rows] if strat_rows else [])

        alerts = []
        for strat in strat_rows:
            s_id = int(strat["id"])
            bal = balances.get("strategies", {}).get(s_id)
            if not bal:
                continue
            ideal_budget = float(bal.get("ideal_budget_usd") or 0.0)
            if ideal_budget <= 0:
                continue

            rules_config = strat.get("rules_config") or {}
            slot_pct = float(rules_config.get("tactic_slot_pct_of_strategy") or 5.0)
            target_slot = ideal_budget * slot_pct / 100.0
            overweight_threshold = target_slot * self.OVERWEIGHT_MULTIPLIER

            sql_positions = f"""
                SELECT a.id AS asset_id, a.listing_id, lt.id AS ticker_id, lt.symbol,
                       sa.allocated_quantity, l.last_price, l.currency_id
                FROM public.strategy_assets sa
                JOIN public.assets a ON sa.asset_id = a.id
                JOIN public.listings l ON a.listing_id = l.id
                JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
                WHERE sa.strategy_id = {s_id} AND sa.allocated_quantity > 0;
            """
            positions = self.db.execute_query(sql_positions) or []
            positions = positions if isinstance(positions, list) else [positions]

            for pos in positions:
                qty = float(pos.get("allocated_quantity") or 0.0)
                last_price = float(pos.get("last_price") or 0.0)
                if qty <= 0 or last_price <= 0:
                    continue
                curr = pos.get("currency_id", "USD")
                value_usd = convert_to_base_currency(self.db, qty * last_price, from_currency=curr)

                if value_usd <= overweight_threshold:
                    continue

                trim_qty = qty * (1 - target_slot / value_usd)

                alerts.append({
                    "asset_id": pos["asset_id"],
                    "portfolio_id": portfolio_id,
                    "listing_id": pos["listing_id"],
                    "ticker_id": pos["ticker_id"],
                    "symbol": pos["symbol"],
                    "strategy_id": s_id,
                    "strategy_name": strat["strategy_name"],
                    "quantity": round(trim_qty, 4),
                    "recommendation": "TRIM",
                    "reason": (
                        f"Позиция выросла до ${value_usd:,.2f} — {value_usd / ideal_budget * 100:.1f}% "
                        f"стратегии, больше {self.OVERWEIGHT_MULTIPLIER:.0f}× планового слота "
                        f"(${target_slot:,.2f}, {slot_pct:.0f}% цели). Подрежь до планового размера, "
                        f"освободившийся капитал пойдёт на диверсификацию."
                    ),
                    "metrics": {
                        "current_value_usd": round(value_usd, 2),
                        "target_slot_usd": round(target_slot, 2),
                        "pct_of_strategy_target": round(value_usd / ideal_budget * 100, 2),
                    },
                    "trigger_kind": "price",
                })

        return alerts
