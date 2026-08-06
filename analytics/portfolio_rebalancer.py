import settings
from analytics.analytics_utils import convert_to_base_currency
from analytics.portfolio_inspector import PortfolioInspector


class PortfolioRebalancer:
    """
    ⚖️ ПОДРЕЗКА/ДОКУПКА ПО ВЕСУ ПОЗИЦИИ (см. Claude/13_portfolio_construction_and_rebalancing_rules.md)
    Сегодня -- только Консервативная. Слои "между стратегиями" и "внутри стратегии по
    весу бумаги" схлопнулись в один механизм (2026-08-03): Трендовая/Револьверная
    корректируют переинвестированность ПАССИВНО (CashDeploymentAdvisor их просто не
    докармливает, см. evaluate_deployment) -- активно продавать здоровую позицию ради
    структуры портфеля им предлагать нельзя, противоречит "жёсткий стоп"/"без
    усреднений вниз" (BACKLOG.md «Сделано» п.33). Консервативной это не касается --
    там нет "тезиса момента", который можно сломать раньше времени.

    Два симметричных триггера, оба относительно планового слота
    (tactic_slot_pct_of_strategy % от целевого бюджета стратегии):
    - TRIM: позиция выросла больше OVERWEIGHT_MULTIPLIER × слота -- подрезать до слота
      (не до нуля -- не полный выход, тем и отличается от PositionExitEvaluator).
      Закрывает исторический разрыв BACKLOG.md п.4 (GLDM-пример 2026-07-23): позиция
      может быть переинвестирована и не давать сигнала от PositionExitEvaluator,
      критерий выхода структурно неприменим (ETF без ROE/долга).
    - TOP_UP: позиция просела ниже UNDERWEIGHT_MULTIPLIER × слота -- добрать до слота.
      Только для позиций БЕЗ активного pipeline (если лесенка ещё не докуплена, этим
      уже занимается LadderStepWatcher -- не дублируем) и только при
      require_confirmed_reversal (тот же signal_ema20_streak_days, что и у обычной
      лесенки, см. TREND_REVERSAL_CONFIRM_DAYS в settings.py) -- иначе это был бы тот
      же MU-баг (докупка в ещё падающий нож), с которого начиналась вся эта тема.
      Ограничено slack стратегии -- не докупаем сверх недоинвестированности.
    """

    OVERWEIGHT_MULTIPLIER = 2.0   # во сколько раз больше планового слота считать перекосом
    UNDERWEIGHT_MULTIPLIER = 0.5  # во сколько раз меньше планового слота считать просадкой

    def __init__(self, db_instance):
        self.db = db_instance

    def evaluate_portfolio(self, portfolio_id: int) -> list:
        inspector = PortfolioInspector(self.db, portfolio_id)
        balances = inspector.get_virtual_cash_balances()

        strat_rows = self.db.execute_query("""
            SELECT s.id, s.strategy_name, s.rules_config
            FROM public.strategies s
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            WHERE s.portfolio_id = %s AND tpl.system_key = 'CONSERVATIVE_ACCUMULATION'
              AND s.is_active = true;
        """, (portfolio_id,))
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
            underweight_threshold = target_slot * self.UNDERWEIGHT_MULTIPLIER
            remaining_slack = float(bal.get("virtual_free_cash_usd") or 0.0)

            sql_positions = """
                SELECT a.id AS asset_id, a.listing_id, lt.id AS ticker_id, lt.symbol,
                       sa.allocated_quantity, l.last_price, l.currency_id, lt.signal_ema20_streak_days,
                       (SELECT op.id FROM public.order_pipelines op
                         WHERE op.portfolio_id = a.portfolio_id AND op.listing_id = a.listing_id
                           AND op.strategy_id = sa.strategy_id AND op.pipeline_status IN ('PENDING', 'ACTIVE')
                           AND op.target_quantity > 0
                         LIMIT 1) AS active_entry_pipeline_id
                FROM public.strategy_assets sa
                JOIN public.assets a ON sa.asset_id = a.id
                JOIN public.listings l ON a.listing_id = l.id
                JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
                WHERE sa.strategy_id = %s AND sa.allocated_quantity > 0;
            """
            positions = self.db.execute_query(sql_positions, (s_id,)) or []
            positions = positions if isinstance(positions, list) else [positions]

            for pos in positions:
                qty = float(pos.get("allocated_quantity") or 0.0)
                last_price = float(pos.get("last_price") or 0.0)
                if qty <= 0 or last_price <= 0:
                    continue
                curr = pos.get("currency_id", "USD")
                value_usd = convert_to_base_currency(self.db, qty * last_price, from_currency=curr)
                base = {
                    "asset_id": pos["asset_id"],
                    "portfolio_id": portfolio_id,
                    "listing_id": pos["listing_id"],
                    "ticker_id": pos["ticker_id"],
                    "symbol": pos["symbol"],
                    "strategy_id": s_id,
                    "strategy_name": strat["strategy_name"],
                    "trigger_kind": "price",
                }

                if value_usd > overweight_threshold:
                    trim_qty = qty * (1 - target_slot / value_usd)
                    alerts.append({
                        **base,
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
                    })
                    continue

                if value_usd >= underweight_threshold or remaining_slack <= 0:
                    continue
                if pos.get("active_entry_pipeline_id"):
                    continue  # лесенка ещё не докуплена -- этим уже занимается LadderStepWatcher
                streak = pos.get("signal_ema20_streak_days")
                if streak is None or int(streak) < settings.TREND_REVERSAL_CONFIRM_DAYS:
                    continue  # разворот не подтверждён -- не докупаем в ещё падающую бумагу

                topup_usd = min(target_slot - value_usd, remaining_slack)
                if topup_usd <= 0:
                    continue
                price_per_share_usd = value_usd / qty
                topup_qty = topup_usd / price_per_share_usd
                remaining_slack -= topup_usd

                alerts.append({
                    **base,
                    "quantity": round(topup_qty, 4),
                    "recommendation": "TOP_UP",
                    "reason": (
                        f"Позиция просела до ${value_usd:,.2f} — {value_usd / ideal_budget * 100:.1f}% "
                        f"стратегии, меньше {self.UNDERWEIGHT_MULTIPLIER:.1f}× планового слота "
                        f"(${target_slot:,.2f}, {slot_pct:.0f}% цели). Разворот подтверждён "
                        f"({int(streak)} дн. выше EMA20) — добери до планового размера."
                    ),
                    "metrics": {
                        "current_value_usd": round(value_usd, 2),
                        "target_slot_usd": round(target_slot, 2),
                        "pct_of_strategy_target": round(value_usd / ideal_budget * 100, 2),
                        "ema20_streak_days": streak,
                    },
                })

        return alerts
