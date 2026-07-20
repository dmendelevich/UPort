#!/usr/bin/env python3
import logging
import datetime


def convert_to_base_currency(db_instance, amount: float, from_currency: str, to_currency: str = "USD") -> float:
    """
    💸 УНИВЕРСАЛЬНЫЙ СКВОЗНОЙ КОНВЕРТЕР АНАЛИТИКИ
    Переводит любую сумму из исходной валюты в базовую (по умолчанию USD).
    Учитывает лондонские мультипликаторы пенсов и актуальные Форекс-курсы СУБД.
    """
    if not amount:
        return 0.0
        
    curr_from = str(from_currency).strip().upper()
    curr_to = str(to_currency).strip().upper()
    
    # Если валюты совпадают, конвертация не требуется
    if curr_from == curr_to:
        return float(amount)
        
    try:
        # 1. Запрашиваем мультипликатор из справочника валют (напр. 0.0100 для GBP/пенсов)
        sql_mult = f"SELECT multiplier FROM public.currencies WHERE id = '{curr_from}' LIMIT 1;"
        res_mult = db_instance.execute_query(sql_mult)
        multiplier = float(res_mult[0]["multiplier"]) if res_mult and res_mult[0].get("multiplier") else 1.0
        
        # 2. Запрашиваем актуальный макрокурс Форекс из таблицы currency_rates
        sql_rate = f"""
            SELECT rate FROM public.currency_rates 
            WHERE from_currency = '{curr_from}' AND to_currency = '{curr_to}' 
            LIMIT 1;
        """
        res_rate = db_instance.execute_query(sql_rate)
        
        if not res_rate:
            logging.warning(f"⚠️ [Analytics Converter]: Курс для пары {curr_from} -> {curr_to} не найден! Используем 1.0")
            rate = 1.0
        else:
            rate = float(res_rate[0]["rate"])
            
        # 🔥 ФИНАНСОВАЯ МАТЕМАТИКА UPORT: применяем мультипликатор, затем умножаем на курс
        clean_usd_value = (float(amount) * multiplier) * rate
        return clean_usd_value

    except Exception as err:
        logging.error(f"❌ [Analytics Converter Error]: Сбой конвертации {curr_from} в {curr_to}: {err}")
        return float(amount)



class TickerEvaluator:
    """
    🔬 УНИВЕРСАЛЬНЫЙ ОЦЕНЩИК БУМАГ UPORT (TickerEvaluator)
    Анализирует соответствие любой акции трем семейным инвестиционным стратегиям.
    Поддерживает иерархию правил: личные настройки стратегии -> глобальный дефолт СУБД.
    Генерирует лог-карты "ПОЧЕМУ" для интерактивных кнопок Telegram-бота.
    """
    
    def __init__(self, db_instance):
        """
        Инициализация оценщика. Кэширует типы данных из словаря аналитических правил.
        """
        self.db = db_instance
        self.global_rules_types = {}
        self._load_rules_vocabulary_types()

    def _load_rules_vocabulary_types(self):
        """
        🔒 ВНУТРЕННИЙ МЕТОД: Кэширует типы данных из analitic_rules_dictionary для конвертации.
        """
        sql = "SELECT sys_name, data_type FROM public.analitic_rules_dictionary;"
        rows = self.db.execute_query(sql) or []
        # Если СУБД вернула список, проходим по нему, если один словарь - берем как есть
        clean_rows = rows if isinstance(rows, list) else [rows]
        for row in clean_rows:
            if row and "sys_name" in row:
                self.global_rules_types[row["sys_name"]] = row["data_type"]

    def _get_rule_value(self, strategy_config: dict, param_key: str, default_val):
        """
        📐 ИЕРАРХИЧЕСКИЙ ШТУРМАН ПАРАМЕТРОВ
        Ищет параметр в JSON-конфиге стратегии. Если его нет, берет системный дефолт.
        """
        if strategy_config and param_key in strategy_config:
            raw_val = strategy_config[param_key]
            p_type = self.global_rules_types.get(param_key, "numeric")
            if p_type == "boolean":
                return bool(raw_val)
            elif p_type == "integer":
                return int(raw_val)
            else:
                return float(raw_val)
        return default_val

    def _calculate_days_to_report(self, report_date_raw) -> int:
        """
        🔒 ВНУТРЕННИЙ МЕТОД: Рассчитывает точное количество дней до следующего отчета.
        """
        if not report_date_raw:
            return 999
        try:
            if isinstance(report_date_raw, str):
                cleaned_date = report_date_raw.split()[0]  # Отсекаем время, если оно есть
                report_date = datetime.datetime.strptime(cleaned_date, "%Y-%m-%d").date()
                return (report_date - datetime.date.today()).days
            elif isinstance(report_date_raw, datetime.date):
                return (report_date_raw - datetime.date.today()).days
            elif isinstance(report_date_raw, datetime.datetime):
                return (report_date_raw.date() - datetime.date.today()).days
        except Exception as err:
            logging.warning(f"⚠️ [Evaluator Date Error]: Не удалось распарсить дату отчета {report_date_raw}: {err}")
        return 999

    def evaluate_ticker_strategy(self, ticker_id: int, target_portfolio_id: int = 2) -> dict:
        """
        🌐 ГЛАВНАЯ СКВОЗНАЯ ТОЧКА ВХОДА ЭВАЛЮАТОРА UPORT
        Сводит воедино все расчеты по трем стратегиям и формирует паспорт актива.
        """
        # 1. ЗАГРУЗКА И РАСПАКОВКА ФАКТОВ О ЦЕННОЙ БУМАГЕ (Железная защита от списков СУБД)
        sql_ticker = f"""
            SELECT 
                id, symbol, company_name, sector, industry, exchange_mic,
                daily_turnover_usd, return_on_equity, debt_to_equity, 
                revenue_cagr_3y, revenue_growth, pe_trailing, dividend_yield, free_cash_flow,
                current_price, target_mean_price, recommendation_mean, signal_next_report_date,
                signal_rsi, signal_macd, signal_ema_20, signal_sma_50, signal_sma_100, signal_sma_200,
                signal_price_to_sma200_pct
            FROM public.tickers WHERE id = {int(ticker_id)} LIMIT 1;
        """
        ticker_res = self.db.execute_query(sql_ticker)
        if not ticker_res:
            return {"error": f"Тикер с ID={ticker_id} не найден в СУБД."}
        
        # Распаковываем чистый словарь из структуры ответа СУБД
        f = ticker_res[0] if isinstance(ticker_res, list) and len(ticker_res) > 0 else (ticker_res if isinstance(ticker_res, dict) else {})
        if not f or "id" not in f:
            return {"error": f"Ошибка распаковки структуры данных тикера ID={ticker_id}"}

        # 2. ЗАГРУЗКА КОНФИГУРАЦИЙ СТРАТЕГИЙ ТЕКУЩЕГО ПОРТФЕЛЯ
        sql_strat = f"SELECT id, rules_config FROM public.strategies WHERE portfolio_id = {int(target_portfolio_id)} AND is_active = true;"
        strat_rows = self.db.execute_query(sql_strat) or []
        clean_strat_rows = strat_rows if isinstance(strat_rows, list) else [strat_rows]
        
        strategy_configs = {}
        for s in clean_strat_rows:
            if s and "id" in s:
                strategy_configs[int(s["id"])] = s["rules_config"] or {}

        # Инициализируем итоговый универсальный Python-словарь паспорта
        evaluation_report = {
            "ticker_id": int(f["id"]),
            "symbol": f.get("symbol"),
            "company_name": f.get("company_name"),
            "compatible_strategies": [],
            "tax_and_limit_alerts": [],
            "explain_map": {}
        }

        # Вытаскиваем сырые технические и фундаментальные метрики
        rsi = float(f.get("signal_rsi") or 0.0)
        macd_val = str(f.get("signal_macd") or "").strip()
        try:
            macd_numeric = float(macd_val) if macd_val and macd_val != "" else 0.0
        except ValueError:
            macd_numeric = 0.0
            
        turnover = float(f.get("daily_turnover_usd") or 0.0)
        fcf = float(f.get("free_cash_flow") or 0.0)
        roe = float(f.get("return_on_equity") or 0.0)
        debt_to_equity = float(f.get("debt_to_equity") or 0.0)
        rev_cagr = float(f.get("revenue_cagr_3y") or 0.0)
        rev_growth = float(f.get("revenue_growth") or 0.0)
        pe = float(f.get("pe_trailing") or 0.0)
        price_to_sma200 = float(f.get("signal_price_to_sma200_pct") or 0.0)
        rec_mean = float(f.get("recommendation_mean") or 0.0)
        curr_price = float(f.get("current_price") or 0.0)
        tgt_price = float(f.get("target_mean_price") or 0.0)
        div_yield_pct = float(f.get("dividend_yield") or 0.0) # В СУБД лежат чистые проценты
        
        days_to_report = self._calculate_days_to_report(f.get("signal_next_report_date"))

        # -------------------------------------------------------------------------
        # СТРАТЕГИЯ 1: РЕВОЛЬВЕРНАЯ СТРАТЕГИЯ (Аудит и формирование лог-карты)
        # -------------------------------------------------------------------------
        if 1 in strategy_configs:
            conf1 = strategy_configs[1]
            limit_turnover1 = self._get_rule_value(conf1, "idea_min_turnover_usd", 500000000.0)
            limit_rsi1 = self._get_rule_value(conf1, "idea_rsi_oversold_num", 45.0)
            limit_buffer1 = self._get_rule_value(conf1, "idea_report_buffer_days", 5)
            require_fcf1 = self._get_rule_value(conf1, "idea_require_positive_fflow_bool", True)
            
            upside_pct = ((tgt_price - curr_price) / curr_price * 100.0) if curr_price > 0 else 0.0

            m1 = {
                "idea_min_turnover_usd": {"status": "PASS" if turnover >= limit_turnover1 else "FAIL", "fact": round(turnover, 2), "limit": limit_turnover1},
                "idea_rsi_oversold_num": {"status": "PASS" if rsi < limit_rsi1 else "FAIL", "fact": rsi, "limit": limit_rsi1},
                "speculative_catalyst": {"status": "PASS" if (macd_numeric > 0 or (0 < rec_mean <= 2.0)) else "FAIL", "fact": f"MACD: {macd_val}, Rec: {rec_mean}", "limit": "MACD > 0 ИЛИ Rec <= 2.0"},
                "idea_require_positive_fflow_bool": {"status": "PASS" if (not require_fcf1 or fcf > 0) else "FAIL", "fact": round(fcf, 2), "limit": "FCF > 0"},
                "idea_report_buffer_days": {"status": "PASS" if days_to_report >= limit_buffer1 else "WARNING", "fact": days_to_report, "limit": limit_buffer1}
            }
            
            is_compat1 = all(x["status"] == "PASS" for k, x in m1.items() if k != "idea_report_buffer_days")
            
            evaluation_report["explain_map"][1] = {
                "strategy_name": "Револьверная стратегия",
                "is_compatible_technically": is_compat1,
                "metrics": m1,
                "ranking_value": round(upside_pct, 2)
            }
            self._apply_tax_and_warning_filters(evaluation_report, 1, is_compat1, config=conf1, div_yield_pct=div_yield_pct, m_report=m1)
        # -------------------------------------------------------------------------
        # СТРАТЕГИЯ 2: КОНСЕРВАТИВНОЕ НАКОПЛЕНИЕ (Аудит и формирование лог-карты)
        # -------------------------------------------------------------------------
        if 2 in strategy_configs:
            conf2 = strategy_configs[2]
            limit_turnover2 = self._get_rule_value(conf2, "idea_min_turnover_usd", 20000000.0)
            limit_buffer2 = self._get_rule_value(conf2, "idea_report_buffer_days", 5)

            m2 = {
                "exchange_mic": {"status": "PASS" if str(f.get("exchange_mic")) in ["XNGS", "XNYS", "XNAS"] else "FAIL", "fact": f.get("exchange_mic"), "limit": "XNGS, XNYS, XNAS"},
                "idea_min_turnover_usd": {"status": "PASS" if turnover >= limit_turnover2 else "FAIL", "fact": round(turnover, 2), "limit": limit_turnover2},
                "return_on_equity": {"status": "PASS" if roe > 0.15 else "FAIL", "fact": round(roe, 4), "limit": "> 0.15"},
                "debt_to_equity": {"status": "PASS" if debt_to_equity < 1.5 else "FAIL", "fact": round(debt_to_equity, 4), "limit": "< 1.5"},
                "revenue_cagr_3y": {"status": "PASS" if rev_cagr > 0.05 else "FAIL", "fact": round(rev_cagr, 4), "limit": "> 0.05"},
                "revenue_growth": {"status": "PASS" if rev_growth > 0.00 else "FAIL", "fact": round(rev_growth, 4), "limit": "> 0.00"},
                "pe_trailing": {"status": "PASS" if pe > 0 else "FAIL", "fact": round(pe, 2), "limit": "> 0"},
                "signal_price_to_sma200_pct": {"status": "PASS" if price_to_sma200 < 0.00 else "FAIL", "fact": price_to_sma200, "limit": "< 0.00"},
                "signal_rsi": {"status": "PASS" if rsi > 35 else "FAIL", "fact": rsi, "limit": "> 35"},
                "idea_report_buffer_days": {"status": "PASS" if days_to_report >= limit_buffer2 else "WARNING", "fact": days_to_report, "limit": limit_buffer2}
            }

            is_compat2 = all(x["status"] == "PASS" for k, x in m2.items() if k != "idea_report_buffer_days")
            rank2 = (roe / debt_to_equity) if debt_to_equity > 0 else roe

            evaluation_report["explain_map"][2] = {
                "strategy_name": "Консервативное накопление",
                "is_compatible_technically": is_compat2,
                "metrics": m2,
                "ranking_value": round(rank2, 4)
            }
            self._apply_tax_and_warning_filters(evaluation_report, 2, is_compat2, config=conf2, div_yield_pct=div_yield_pct, m_report=m2)

        # -------------------------------------------------------------------------
        # СТРАТЕГИЯ 3: СЛЕДОВАНИЕ ЗА ТРЕНДОМ («Поезда», Аудит и лог-карта)
        # -------------------------------------------------------------------------
        if 3 in strategy_configs:
            conf3 = strategy_configs[3]
            limit_turnover3 = self._get_rule_value(conf3, "idea_min_turnover_usd", 100000000.0)
            limit_buffer3 = self._get_rule_value(conf3, "idea_report_buffer_days", 5)

            # Извлекаем скользящие средние для жесткого веера
            ema20 = float(f.get("signal_ema_20") or 0.0)
            sma50 = float(f.get("signal_sma_50") or 0.0)
            sma100 = float(f.get("signal_sma_100") or 0.0)
            sma200 = float(f.get("signal_sma_200") or 0.0)
            fan_is_valid = (ema20 > sma50) and (sma50 > sma100) and (sma100 > sma200)

            # Эмуляция трендового всплеска объемов торгов (по ликвидности > 100M)
            volume_confirmed = True if turnover > 100000000 else False

            m3 = {
                "idea_min_turnover_usd": {"status": "PASS" if turnover >= limit_turnover3 else "FAIL", "fact": round(turnover, 2), "limit": limit_turnover3},
                "moving_averages_fan": {"status": "PASS" if fan_is_valid else "FAIL", "fact": f"EMA20={ema20}, SMA50={sma50}, SMA100={sma100}, SMA200={sma200}", "limit": "EMA20 > SMA50 > SMA100 > SMA200"},
                "signal_price_to_sma200_pct": {"status": "PASS" if price_to_sma200 > 5.00 else "FAIL", "fact": price_to_sma200, "limit": "> 5.00%"},
                "signal_rsi": {"status": "PASS" if (50.0 <= rsi <= 72.0) else "FAIL", "fact": rsi, "limit": "50 - 72"},
                "signal_macd": {"status": "PASS" if macd_numeric > 0 else "FAIL", "fact": macd_numeric, "limit": "> 0"},
                "tactic_volume_surge_pct": {"status": "PASS" if volume_confirmed else "FAIL", "fact": "Объем подтвержден" if volume_confirmed else "Низкий оборот", "limit": "Всплеск объема торгов"},
                "idea_report_buffer_days": {"status": "PASS" if days_to_report >= limit_buffer3 else "WARNING", "fact": days_to_report, "limit": limit_buffer3}
            }

            is_compat3 = all(x["status"] == "PASS" for k, x in m3.items() if k != "idea_report_buffer_days")

            evaluation_report["explain_map"][3] = {
                "strategy_name": "Стратегия следования за трендом",
                "is_compatible_technically": is_compat3,
                "metrics": m3,
                "ranking_value": price_to_sma200
            }
            self._apply_tax_and_warning_filters(evaluation_report, 3, is_compat3, config=conf3, div_yield_pct=div_yield_pct, m_report=m3)

        return evaluation_report

    def _apply_tax_and_warning_filters(self, evaluation_report: dict, strategy_id: int, is_compatible: bool, config: dict, div_yield_pct: float, m_report: dict):
        """
        🔒 СЛУЖЕБНЫЙ МЕТОД ФИЛЬТРАЦИИ И ВЫРАБОТКИ ПРЕДУПРЕЖДЕНИЙ ШТУРМАНА
        """
        strat_name = evaluation_report["explain_map"][strategy_id]["strategy_name"]
        
        if is_compatible:
            # Налоговый рентген
            max_div_allowed = config.get("portfolio_max_allowed_div_pct")
            if max_div_allowed is not None and div_yield_pct > float(max_div_allowed):
                evaluation_report["compatible_strategies"].append(strategy_id)
                evaluation_report["tax_and_limit_alerts"].append(
                    f"⚠️ НАЛОГОВЫЙ ПРЕДОХРАНИТЕЛЬ [{strat_name}]: Дивиденды {round(div_yield_pct, 2)}% "
                    f"выше лимита {float(max_div_allowed)}%. Ожидаются налоги до 30%!"
                )
            else:
                evaluation_report["compatible_strategies"].append(strategy_id)

        # Календарный предохранитель отчетов
        if m_report.get("idea_report_buffer_days", {}).get("status") == "WARNING":
            days = m_report["idea_report_buffer_days"]["fact"]
            evaluation_report["tax_and_limit_alerts"].append(
                f"📅 [{strat_name}]: РИСК ОТЧЕТА! До финансовых результатов осталось всего {days} дней. Будет штормить!"
            )