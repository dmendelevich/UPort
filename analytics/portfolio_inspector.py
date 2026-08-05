#!/usr/bin/env python3
import logging
from analytics.analytics_utils import convert_to_base_currency, check_sector_ceiling_breach, CONTENT_STRATEGY_SYSTEM_KEYS

class PortfolioInspector:
    """
    🔬 ГЛАВНЫЙ АНАЛИТИЧЕСКИЙ ИНСПЕКТОР UPORT
    Отвечает за аудит, инспекцию лимитов и расчет виртуальных балансов портфелей.
    Изолирован от интерфейсов, работает напрямую со шлюзом СУБД.
    """
    
    def __init__(self, db_instance, portfolio_id: int):
        """
        Инициализация инспектора. Сразу собирает всю сырую конфигурацию из СУБД.
        """
        self.db = db_instance
        self.portfolio_id = int(portfolio_id)
        
        # Внутренние хранилища сырых фактов портфеля
        self.raw_accounts = []      # Реальные счета брокера (торговый и накопительный D)
        self.raw_strategies = []    # Активные семейные стратегии, их rules_config и СУБД-доли
        self.raw_assets = []        # Физический состав ценных бумаг на счете
        self.sector_target_config = {}  # Потолки по секторам ДЛЯ ЭТОГО портфеля (Claude/BACKLOG.md №82)

        # Автоматический экспресс-сбор данных при создании объекта
        self._collect_raw_portfolio_facts()

    def _collect_raw_portfolio_facts(self):
        """
        🔒 ВНУТРЕННИЙ МЕТОД: Зряче собирает факты из СУБД, связывая торговые и накопительные счета.
        """
        logging.info(f"🔍 [PortfolioInspector]: Сбор фактов для портфеля ID={self.portfolio_id}...")
        
        # 1. Шаг А: Узнаем базовый номер торгового счета для этого портфеля (ищем USD-строку)
        sql_base_acc = f"""
            SELECT account_number FROM public.accounts 
            WHERE portfolio_id = {self.portfolio_id} AND account_type = 'trade' AND currency_id = 'USD' 
            LIMIT 1;
        """
        base_res = self.db.execute_query(sql_base_acc)
        base_row = base_res[0] if isinstance(base_res, list) and len(base_res) > 0 else (base_res if isinstance(base_res, dict) else {})
        base_account_number = base_row.get("account_number") if base_row else None
        
        if not base_account_number:
            logging.warning(f"⚠️ [PortfolioInspector]: Не найден базовый торговый счет для ID={self.portfolio_id}")
            sql_accounts = f"SELECT * FROM public.accounts WHERE portfolio_id = {self.portfolio_id};"
        else:
            clean_base = str(base_account_number).strip()
            deposit_variant = "D" + clean_base
            
            sql_accounts = f"""
                SELECT id, account_number, account_type, currency_id, 
                       cash_available, cash_reserved, assets_value 
                FROM public.accounts 
                WHERE account_number IN ('{clean_base}', '{deposit_variant}');
            """
            
        self.raw_accounts = self.db.execute_query(sql_accounts) or []
        
        # 2. ЗАПРОС К ПРАВИЛАМ СТРАТЕГИЙ (🔥 ТЕПЕРЬ ТАЩИМ СКОРРЕКТИРОВАННУЮ КОЛОНКУ С ДОЛЯМИ СУБД)
        # system_key нужен audit_limits_and_rules -- отличить служебную Неопределённую
        # стратегию от содержательных (см. Claude/BACKLOG.md, обсуждение 2026-07-27).
        sql_strategies = f"""
            SELECT s.id, s.strategy_name, s.strategy_share_pct, s.rules_config, s.human_philosophy, st.system_key
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = {self.portfolio_id} AND s.is_active = true;
        """
        self.raw_strategies = self.db.execute_query(sql_strategies) or []

        # 2б. Потолки по секторам ДЛЯ ЭТОГО портфеля (Claude/BACKLOG.md №82)
        portfolio_row = self.db.execute_row(
            "SELECT sector_target_config FROM public.portfolios WHERE id = %s;",
            (self.portfolio_id,)
        )
        self.sector_target_config = (portfolio_row or {}).get("sector_target_config") or {}

        # 3. ЗАПРОС К АКТИВАМ БРОКЕРА
        sql_assets = f"""
            SELECT id, listing_id, quantity, avg_price, currency_id 
            FROM public.assets 
            WHERE portfolio_id = {self.portfolio_id} AND quantity > 0;
        """
        self.raw_assets = self.db.execute_query(sql_assets) or []
        
        logging.info(
            f"✅ [PortfolioInspector Факты зафиксированы]: Счетов подтянуто: {len(self.raw_accounts)}, "
            f"Активных стратегий: {len(self.raw_strategies)}, Бумаг в assets: {len(self.raw_assets)}"
        )

    def calculate_total_capital(self) -> float:
        """
        💸 ГЛОБАЛЬНЫЙ ДЕНЕЖНЫЙ КОТЕЛ (С ПОШАГОВОЙ ОТЛАДКОЙ)
        Рассчитывает полную рыночную стоимость портфеля в USD цент в цент.
        Пошаговый разбор -- на DEBUG (Claude/BACKLOG.md №27/28): вызывается по несколько раз за
        один дайджест/подтверждение сделки, раньше безусловно печатался в лог через print().
        """
        logging.debug(" ─── [СТАРТ ОТЛАДКИ CALCULATE_TOTAL_CAPITAL] ───")
        total_cash_usd = 0.0

        # 1. Шаг А: Пошаговый разбор кэша со счетов брокера
        for acc in self.raw_accounts:
            acc_num = acc.get("account_number")
            acc_type = acc.get("account_type")
            cash_val = float(acc.get("cash_available") or 0.0)
            curr = acc.get("currency_id", "USD")

            cash_usd = convert_to_base_currency(self.db, cash_val, from_currency=curr)
            total_cash_usd += cash_usd
            logging.debug(f"   • Счет {acc_num} ({acc_type}) | Факт в СУБД: {cash_val} {curr} -> В базовых USD: ${cash_usd:,.2f}")

        logging.debug(f" 🟩 [ИТОГ ПО КЭШУ]: Общий свободный кэш портфеля = ${total_cash_usd:,.2f}")

        # 2. Шаг Б: Пошаговый разбор рыночной стоимости акций из VIEW
        sql_market_assets = f"""
            SELECT COALESCE(SUM(se.exposure_usd), 0.0) AS total_market_assets_usd
            FROM public.strategy_exposure se
            WHERE se.strategy_id IN (
                SELECT id FROM public.strategies WHERE portfolio_id = {self.portfolio_id} AND is_active = true
            );
        """
        assets_res = self.db.execute_query(sql_market_assets)
        
        if isinstance(assets_res, list) and len(assets_res) > 0:
            row = assets_res[0]
        elif isinstance(assets_res, dict):
            row = assets_res
        else:
            row = {}
            
        total_assets_market_usd = float(row.get("total_market_assets_usd") or 0.0)
        logging.debug(f" 📈 [ИТОГ ПО АКЦИЯМ]: Стоимость всех ценных бумаг из VIEW = ${total_assets_market_usd:,.2f}")

        # 3. Шаг В: Финальное сложение
        total_capital = total_cash_usd + total_assets_market_usd
        logging.debug(f" 🟪 [ФИНАЛЬНЫЙ СУММАРНЫЙ КАПИТАЛ]: ${total_cash_usd:,.2f} + ${total_assets_market_usd:,.2f} = ${total_capital:,.2f}")

        return round(total_capital, 2)


    def get_virtual_cash_balances(self) -> dict:
        """
        📐 СЕМЕЙНЫЙ ВИРТУАЛЬНЫЙ РЕНТГЕН (ОБНОВЛЕННЫЙ)
        Рассчитывает свободный кэш для каждой стратегии (включая Кэш/Резерв).
        🔥 ТЕПЕРЬ ТАЩИТ ДОЛИ НАПРЯМУЮ ИЗ СУБД СТРОКОЙ СТРАТЕГИИ!
        """
        total_capital = self.calculate_total_capital()
        if total_capital <= 0:
            return {"strategies": {}, "total_capital_usd": 0.0}
            
        virtual_report = {
            "total_capital_usd": total_capital,
            "strategies": {}
        }
        
        # 1. Шаг А: Считаем идеальные бюджеты и суммируем факт купленных акций по стратегиям
        for strat in self.raw_strategies:
            s_id = strat["id"]
            s_name = strat["strategy_name"]
            
            # Берем долю стратегии напрямую из физического поля СУБД
            share_pct = float(strat.get("strategy_share_pct") or 0.0)
            
            # Идеальный бюджет в долларах, выделенный семьей под эту стратегию
            ideal_budget_usd = total_capital * (share_pct / 100.0)
            
            # 🔥 ТОЧЕЧНОЕ ИСПРАВЛЕНИЕ UPORT: Вытаскиваем количество акций стратегии 
            # и актуальную ценуlast_price из таблицы листингов в валюте брокера
            sql_strategy_positions = f"""
                SELECT sa.allocated_quantity, l.last_price, l.currency_id
                FROM public.strategy_assets sa
                JOIN public.assets a ON sa.asset_id = a.id
                JOIN public.listings l ON a.listing_id = l.id
                WHERE sa.strategy_id = {s_id} AND sa.allocated_quantity > 0;
            """
            positions_res = self.db.execute_query(sql_strategy_positions) or []
            clean_positions = positions_res if isinstance(positions_res, list) else [positions_res]
            
            # Считаем текущую рыночную стоимость активов стратегии с приведением к USD
            current_holdings_usd = 0.0
            for pos in clean_positions:
                qty = float(pos.get("allocated_quantity") or 0.0)
                last_price = float(pos.get("last_price") or 0.0)
                curr = pos.get("currency_id", "USD")
                
                # Рыночная стоимость позиции в валюте листинга (учитывает пенсы Trading212)
                position_raw_value = qty * last_price
                
                # Конвертируем в базовые доллары США через наш сквозной конвертер
                position_usd = convert_to_base_currency(self.db, position_raw_value, from_currency=curr)
                current_holdings_usd += position_usd
            
            # 🔥 ФОРМУЛА UPORT: Свободный виртуальный кэш стратегии
            virtual_free_cash = ideal_budget_usd - current_holdings_usd
            
            virtual_report["strategies"][s_id] = {
                "strategy_name": s_name,
                "target_share_pct": share_pct,
                "ideal_budget_usd": round(ideal_budget_usd, 2),
                "current_holdings_usd": round(current_holdings_usd, 2),
                "virtual_free_cash_usd": round(virtual_free_cash, 2)
            }
            
        return virtual_report

    def get_portfolio_sector_exposure(self) -> dict:
        """
        🌐 ПОРТФЕЛЬНЫЙ СЕКТОРАЛЬНЫЙ РЕНТГЕН (Claude/BACKLOG.md №82)
        Сумма $-экспозиции по сектору across ВСЕХ активных содержательных стратегий
        портфеля разом (REVOLVER/CONSERVATIVE_ACCUMULATION/TREND_FOLLOWING) -- не
        внутри одной, как в audit_limits_and_rules ниже. UNALLOCATED/CASH_RESERVE
        не участвуют (первая -- само наличие бумаг там уже нарушение по другой
        причине, вторая -- в ней физически нет активов). Та же честная ETF-
        декомпозиция через strategy_exposure, что и везде.
        Возвращает {sector: usd}, пустой словарь если участвовать нечему.
        """
        content_ids = [
            int(s["id"]) for s in self.raw_strategies
            if s.get("system_key") in CONTENT_STRATEGY_SYSTEM_KEYS
        ]
        if not content_ids:
            return {}

        ids_str = ", ".join(str(i) for i in content_ids)
        sql = f"""
            SELECT COALESCE(t.sector, 'Unknown Sector') AS sector, SUM(se.exposure_usd) AS total_usd
            FROM public.strategy_exposure se
            JOIN public.tickers t ON se.ticker_id = t.id
            WHERE se.strategy_id IN ({ids_str}) AND se.exposure_usd > 0
            GROUP BY COALESCE(t.sector, 'Unknown Sector');
        """
        rows = self.db.execute_query(sql) or []
        rows = rows if isinstance(rows, list) else [rows]
        return {r["sector"]: float(r["total_usd"]) for r in rows if r}

    def audit_limits_and_rules(self) -> dict:
        """
        🔬 КОМПЛЕКСНЫЙ ИНСПЕКТОР ЛИМИТОВ И ПРАВИЛ UPORT (ОБНОВЛЕННЫЙ)
        Проверяет каждую активную стратегию на основе данных из VIEW.
        Учитывает прямые акции и скрытые доли внутри ETF цент в цент.
        """
        # Шаг 1: Получаем живой рыночный баланс капитала портфеля
        virtual_data = self.get_virtual_cash_balances()
        total_capital = virtual_data["total_capital_usd"]
        
        if total_capital <= 0:
            return {"portfolio_id": self.portfolio_id, "has_violations": False, "strategies": {}}

        audit_report = {
            "portfolio_id": self.portfolio_id,
            "has_violations": False,
            "strategies": {}
        }

        # Шаг 2: Проходим зрячим циклом по каждой активной стратегии
        for strat in self.raw_strategies:
            s_id = strat["id"]
            s_name = strat["strategy_name"]
            config = strat.get("rules_config") or {}
            
            # Извлекаем жесткие лимиты рисков из конфигурации JSON
            max_asset_pct = float(config.get("portfolio_max_asset_pct", 5.0))
            max_sector_pct = float(config.get("portfolio_max_sector_pct", 25.0))
            
            strat_report = {
                "strategy_name": s_name,
                "violation_found": False,
                "violated_assets": [],
                "violated_sectors": [],
                "tax_shield_breaches": [],
                "unassigned_assets": []
            }

            # 🔥 РЕФОРМА ШАГА 3: Элегантный запрос к нашему новому СУБД VIEW
            # База данных сама выдает готовые, декомпозированные доллары по каждому тикеру
            sql_strategy_exposure = f"""
                SELECT 
                    t.symbol, 
                    COALESCE(t.sector, 'Unknown Sector') AS sector, 
                    t.dividend_yield, 
                    se.exposure_usd AS total_usd
                FROM public.strategy_exposure se
                JOIN public.tickers t ON se.ticker_id = t.id
                WHERE se.strategy_id = {s_id} AND se.exposure_usd > 0;
            """
            exposure_shares = self.db.execute_query(sql_strategy_exposure) or []
            
            sector_totals_usd = {}

            # Шаг 4: Проверяем каждую акцию по её АБСОЛЮТНОЙ рыночной стоимости (Прямая + ETF)
            for asset in exposure_shares:
                symbol = asset["symbol"]
                sector = asset["sector"]
                div_yield = float(asset["dividend_yield"] or 0.0)
                
                # Забираем уже готовые, пересчитанные базовые доллары из СУБД
                asset_usd = float(asset["total_usd"])

                # Вычисляем реальный текущий процент акции от ВСЕГО капитала портфеля
                asset_share_pct = (asset_usd / total_capital) * 100.0

                # Неопределённая стратегия -- служебный "карман", в ней вообще не должно быть
                # бумаг: сам факт наличия позиции уже нарушение, обычные лимиты по доле/сектору/
                # дивидендам для неё не проверяем (у неё нет содержательного rules_config).
                if strat.get("system_key") == "UNALLOCATED":
                    strat_report["violation_found"] = True
                    audit_report["has_violations"] = True
                    strat_report["unassigned_assets"].append({
                        "symbol": symbol,
                        "current_share_pct": round(asset_share_pct, 2)
                    })
                    continue

                # Накопление для идеального секторального рентгена
                sector_totals_usd[sector] = sector_totals_usd.get(sector, 0.0) + asset_usd

                # А: ПРОВЕРКА ЛИМИТА НА АКЦИЮ (например, > 5% с учетом ETF)
                if asset_share_pct > max_asset_pct:
                    strat_report["violation_found"] = True
                    audit_report["has_violations"] = True
                    strat_report["violated_assets"].append({
                        "symbol": symbol,
                        "current_share_pct": round(asset_share_pct, 2),
                        "limit_pct": max_asset_pct
                    })

                # Б: ПРОВЕРКА ГИБКОГО НАЛОГОВОГО ЩИТА (Данные в СУБД уже в %%)
                max_div_allowed = config.get("portfolio_max_allowed_div_pct")
                div_yield_pct = div_yield 

                if max_div_allowed is not None and div_yield_pct > float(max_div_allowed):
                    strat_report["violation_found"] = True
                    audit_report["has_violations"] = True
                    strat_report["tax_shield_breaches"].append({
                        "symbol": symbol,
                        "dividend_yield_pct": round(div_yield_pct, 2),
                        "limit_pct": float(max_div_allowed)
                    })

            # Шаг 5: СЕКТОРАЛЬНЫЙ РЕНТГЕН (Проверка лимита на сектор от живого капитала)
            for sector_name, sector_usd in sector_totals_usd.items():
                sector_share_pct = (sector_usd / total_capital) * 100.0
                
                if sector_share_pct > max_sector_pct:
                    strat_report["violation_found"] = True
                    audit_report["has_violations"] = True
                    strat_report["violated_sectors"].append({
                        "sector": sector_name,
                        "current_share_pct": round(sector_share_pct, 2),
                        "limit_pct": max_sector_pct
                    })

            # Шаг 6: Сохраняем результаты по текущей стратегии в общий отчет
            audit_report["strategies"][s_id] = strat_report

        # Шаг 7: ПОРТФЕЛЬНЫЙ СЕКТОРАЛЬНЫЙ РЕНТГЕН (Claude/BACKLOG.md №82) -- НЕЗАВИСИМЫЙ
        # контур поверх постратегийного выше: сумма по ВСЕМ активным стратегиям сразу,
        # против sector_target_config портфеля, а не rules_config одной стратегии.
        portfolio_sector_exposure = self.get_portfolio_sector_exposure()
        audit_report["portfolio_violated_sectors"] = []
        for sector_name in portfolio_sector_exposure:
            breach = check_sector_ceiling_breach(
                portfolio_sector_exposure, total_capital, self.sector_target_config, sector_name
            )
            if breach:
                audit_report["has_violations"] = True
                audit_report["portfolio_violated_sectors"].append(breach)

        return audit_report
