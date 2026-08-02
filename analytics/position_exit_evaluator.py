import datetime

import settings


class PositionExitEvaluator:
    """
    🚪 ОЦЕНЩИК ВЫХОДА ИЗ ПОЗИЦИЙ UPORT
    Сканирует уже открытые позиции портфеля и проверяет, не сработало ли правило
    выхода той стратегии, к которой бумага сейчас привязана в strategy_assets.
    Только рекомендации (SELL/HOLD с обоснованием) — никаких действий с ордерами.
    """

    def __init__(self, db_instance):
        self.db = db_instance

    def _get_rule_value(self, rules_config: dict, key: str, default_val: float):
        if rules_config and key in rules_config and rules_config[key] is not None:
            return float(rules_config[key])
        return default_val

    def _days_since(self, dt_raw) -> int:
        if not dt_raw:
            return 0
        try:
            if isinstance(dt_raw, str):
                dt = datetime.datetime.fromisoformat(dt_raw)
            else:
                dt = dt_raw
            d = dt.date() if isinstance(dt, datetime.datetime) else dt
            return (datetime.date.today() - d).days
        except Exception:
            return 0

    def evaluate_portfolio_exits(self, portfolio_id: int) -> list:
        """
        Главная точка входа. Возвращает список алертов по позициям портфеля,
        для которых сработало правило выхода их стратегии (не для всех позиций,
        только для тех, где есть что сказать).
        """
        sql = f"""
            SELECT
                a.id AS asset_id, a.portfolio_id, a.listing_id, a.quantity, a.avg_price, a.position_opened_at,
                lt.id AS ticker_id, lt.symbol, lt.current_price,
                lt.return_on_equity, lt.debt_to_equity, lt.pe_trailing,
                lt.signal_rsi, lt.signal_macd, lt.signal_ema_20, lt.signal_sma_50, lt.signal_ema20_streak_days,
                s.id AS strategy_id, s.strategy_name, s.rules_config,
                tpl.system_key, op.id AS active_entry_pipeline_id
            FROM public.assets a
            JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
            JOIN public.strategy_assets sa ON sa.asset_id = a.id AND sa.allocated_quantity > 0
            JOIN public.strategies s ON sa.strategy_id = s.id
            JOIN public.strategy_templates tpl ON s.template_id = tpl.id
            LEFT JOIN public.order_pipelines op
                ON op.portfolio_id = a.portfolio_id AND op.listing_id = a.listing_id
               AND op.strategy_id = s.id AND op.pipeline_status IN ('PENDING', 'ACTIVE')
               AND op.target_quantity > 0
            WHERE a.portfolio_id = {int(portfolio_id)} AND a.quantity > 0;
        """
        rows = self.db.execute_query(sql) or []
        clean_rows = rows if isinstance(rows, list) else [rows]

        alerts = []
        for pos in clean_rows:
            if not pos:
                continue

            system_key = pos.get("system_key")
            if system_key == "REVOLVER":
                alert = self._check_revolver_exit(pos)
            elif system_key == "CONSERVATIVE_ACCUMULATION":
                alert = self._check_conservative_exit(pos)
            elif system_key == "TREND_FOLLOWING":
                alert = self._check_trend_exit(pos)
            else:
                # CASH_RESERVE / UNALLOCATED -- тактики выхода не определено, пропускаем
                continue

            if alert:
                alerts.append(alert)

        return alerts

    def _build_alert(self, pos: dict, recommendation: str, reason: str, metrics: dict, trigger_kind: str = "price") -> dict:
        return {
            "asset_id": pos.get("asset_id"),
            "portfolio_id": pos.get("portfolio_id"),
            "listing_id": pos.get("listing_id"),
            "ticker_id": pos.get("ticker_id"),
            "symbol": pos.get("symbol"),
            "strategy_id": pos.get("strategy_id"),
            "strategy_name": pos.get("strategy_name"),
            "system_key": pos.get("system_key"),
            "quantity": float(pos.get("quantity") or 0.0),
            "recommendation": recommendation,  # "SELL" | "HOLD"
            "reason": reason,
            "metrics": metrics,
            # "price" -- сработало от цены/RSI/фундаментала; "calendar" -- сработало от
            # прошедшего времени (напр. тайм-аут слота Револьверной), см. Claude/BACKLOG.md
            # (реорганизация дайджеста на разделы "по расписанию" vs "сигналы", 2026-07-30).
            "trigger_kind": trigger_kind,
        }

    def _check_revolver_exit(self, pos: dict):
        # Цена входа берётся из assets.avg_price, а не order_pipelines.initial_entry_price:
        # на живых данных order_pipelines для большинства держимых позиций отсутствует или
        # содержит 0.0, а Револьверная тактика — всегда один шаг на 100% бюджета, поэтому
        # avg_price эквивалентен цене первого входа.
        entry_price = float(pos.get("avg_price") or 0.0)
        current_price = float(pos.get("current_price") or 0.0)
        if entry_price <= 0 or current_price <= 0:
            return None

        rules = pos.get("rules_config") or {}
        target_profit_pct = self._get_rule_value(rules, "tactic_target_profit_pct", 15.0)
        time_limit_days = int(self._get_rule_value(rules, "tactic_time_limit_days", 30))
        trend_protection_rsi = self._get_rule_value(rules, "tactic_trend_protection_rsi", 65.0)

        profit_pct = (current_price - entry_price) / entry_price * 100.0
        rsi = float(pos.get("signal_rsi") or 0.0)
        days_held = self._days_since(pos.get("position_opened_at"))
        metrics = {"profit_pct": round(profit_pct, 2), "rsi": rsi, "days_held": days_held}

        if profit_pct >= target_profit_pct:
            if rsi > trend_protection_rsi:
                return self._build_alert(
                    pos, "HOLD",
                    f"Цель +{target_profit_pct:.1f}% достигнута (факт {profit_pct:.1f}%), но RSI={rsi:.1f} "
                    f"выше {trend_protection_rsi:.1f} — импульс ещё жив. Не продавай, придержи и рассмотри "
                    f"перевод в Трендовую стратегию вручную.",
                    metrics,
                )
            return self._build_alert(
                pos, "SELL",
                f"Цель +{target_profit_pct:.1f}% достигнута (факт {profit_pct:.1f}%), импульс остыл "
                f"(RSI={rsi:.1f}) — фиксируй прибыль.",
                metrics,
            )

        if days_held >= time_limit_days:
            return self._build_alert(
                pos, "SELL",
                f"Тайм-аут слота: {days_held} дн. с открытия позиции (лимит {time_limit_days}), цель "
                f"+{target_profit_pct:.1f}% не достигнута (факт {profit_pct:.1f}%). Освобождай слот.",
                metrics,
                trigger_kind="calendar",
            )

        return None

    def _check_conservative_exit(self, pos: dict):
        # NULL в фундаментальных полях означает "не применимо" (напр. ETF без ROE/долга),
        # а не "ноль" -- отсутствующий показатель просто пропускает свою проверку,
        # а не превращается в ложное "фундаментал сломался".
        roe_raw = pos.get("return_on_equity")
        debt_to_equity_raw = pos.get("debt_to_equity")
        pe_trailing_raw = pos.get("pe_trailing")

        reasons = []
        if roe_raw is not None and float(roe_raw) < 0.10:
            reasons.append(f"ROE упал до {float(roe_raw) * 100:.1f}% (порог 10%)")
        if debt_to_equity_raw is not None and float(debt_to_equity_raw) > 2.0:
            reasons.append(f"Долг/Капитал вырос до {float(debt_to_equity_raw):.2f} (порог 2.0)")
        if pe_trailing_raw is not None and float(pe_trailing_raw) < 0:
            reasons.append("компания в убытке (P/E отрицательный)")

        if reasons:
            return self._build_alert(
                pos, "SELL",
                "Фундаментал сломался: " + "; ".join(reasons) + ". Рекомендация: ликвидировать позицию.",
                {
                    "roe": float(roe_raw) if roe_raw is not None else None,
                    "debt_to_equity": float(debt_to_equity_raw) if debt_to_equity_raw is not None else None,
                    "pe_trailing": float(pe_trailing_raw) if pe_trailing_raw is not None else None,
                },
            )

        # "Сдаться": лесенка ещё не докуплена (есть незавершённый план входа) и подтверждённого
        # разворота (settings.TREND_REVERSAL_CONFIRM_DAYS) так и не случилось дольше
        # tactic_give_up_days -- см. Claude/12_investment_goal_and_mechanisms_roadmap.md (MU-кейс:
        # купили на отскок, отскока нет, ждать вечно бессмысленно, даже если фундаментал в порядке).
        if pos.get("active_entry_pipeline_id"):
            rules = pos.get("rules_config") or {}
            give_up_days = int(self._get_rule_value(rules, "tactic_give_up_days", 30))
            days_held = self._days_since(pos.get("position_opened_at"))
            streak = pos.get("signal_ema20_streak_days")
            reversal_confirmed = streak is not None and int(streak) >= settings.TREND_REVERSAL_CONFIRM_DAYS

            if days_held >= give_up_days and not reversal_confirmed:
                return self._build_alert(
                    pos, "SELL",
                    f"Лесенка не докуплена {days_held} дн. (лимит {give_up_days}), подтверждённого "
                    f"разворота всё ещё нет — ставка не сыграла, сдаться и освободить капитал.",
                    {"days_held": days_held, "give_up_days": give_up_days, "ema20_streak_days": streak},
                    trigger_kind="calendar",
                )

        return None

    def _check_trend_exit(self, pos: dict):
        current_price = float(pos.get("current_price") or 0.0)
        ema20 = float(pos.get("signal_ema_20") or 0.0)
        sma50 = float(pos.get("signal_sma_50") or 0.0)
        rsi = float(pos.get("signal_rsi") or 0.0)
        streak = pos.get("signal_ema20_streak_days")
        macd_val = str(pos.get("signal_macd") or "").strip()
        try:
            macd = float(macd_val) if macd_val else 0.0
        except ValueError:
            macd = 0.0

        rsi_overheat = 80.0
        metrics = {"current_price": current_price, "ema20": ema20, "sma50": sma50, "rsi": rsi, "macd": macd, "ema20_streak_days": streak}

        # Подтверждённый слом (settings.TREND_REVERSAL_CONFIRM_DAYS дней подряд ниже EMA20),
        # не сырое однодневное пересечение -- см. Claude/12_investment_goal_and_mechanisms_roadmap.md
        # (живой пример VRTX/BA, где однодневный кросс ничего не значил, против MU/META, где
        # серия уже устойчиво держится много дней).
        if streak is not None and int(streak) <= -settings.TREND_REVERSAL_CONFIRM_DAYS:
            return self._build_alert(
                pos, "SELL",
                f"Цена держится ниже EMA20 ({ema20:.2f}) уже {abs(int(streak))} торговых дней подряд "
                f"(текущая {current_price:.2f}) — подтверждённый слом тренда.",
                metrics,
            )

        if ema20 > 0 and sma50 > 0 and ema20 < sma50:
            return self._build_alert(
                pos, "SELL",
                f"EMA20 ({ema20:.2f}) ушла ниже SMA50 ({sma50:.2f}) — разворот тренда.",
                metrics,
            )

        if rsi > rsi_overheat and macd < 0:
            return self._build_alert(
                pos, "SELL",
                f"Экстремальный перегрев: RSI={rsi:.1f} (>{rsi_overheat:.0f}), MACD развернулся в минус ({macd:.3f}).",
                metrics,
            )

        return None
