#!/usr/bin/env python3
import logging
import datetime
import settings

# "Содержательные" стратегии -- в них реально покупаются/продаются бумаги по торговой
# логике. Кэш/Резерв и Неопределённая -- служебные "карманы" портфеля, без входа/выхода
# и без собственного смысла для метрик вроде "сколько у меня стратегий" (см. обсуждение
# карточки портфеля, Claude/BACKLOG.md). Общая точка, чтобы не разъезжалось между экранами.
CONTENT_STRATEGY_SYSTEM_KEYS = ("REVOLVER", "CONSERVATIVE_ACCUMULATION", "TREND_FOLLOWING", "INDEX_CORE")


def expected_step_quantity(target_qty: float, budget_share_pct: float) -> int:
    """
    Ожидаемый объём (в штуках, со знаком) текущего шага лесенки order_pipelines:
    доля от итоговой цели плана, знак -- как у самого плана (>0 вход, <0 выход).
    Не может быть меньше 1 целой акции по модулю. Общая для sync_strategy_asset_fb.py
    (сверка факта) и digest-наблюдателя следующего шага (analytics/ladder_step_watcher.py) --
    держим в одном месте, чтобы не разъезжались.
    """
    target_qty = float(target_qty)
    sign = 1 if target_qty > 0 else -1
    raw_step_qty = target_qty * (float(budget_share_pct) / 100.0)
    return sign * max(1, round(abs(raw_step_qty)))


def normalize_debt_to_equity(raw_value):
    """
    Yahoo отдаёт debtToEquity в процентных пунктах (17.6 значит коэффициент 0.176), не
    голым мультипликатором -- проверено сверкой с реальным балансом по нескольким тикерам
    (2026-08-06). `tickers.debt_to_equity` хранит сырое значение источника как есть (тот же
    принцип, что и `currencies.multiplier` -- канонический слой в нативных единицах, перевод
    в общепринятый коэффициент делается здесь, в момент использования, единой точкой).
    """
    return float(raw_value) / 100.0 if raw_value is not None else None


def na_or_check(raw_val, check_fn):
    """
    NULL в фундаментальном/сигнальном поле = "неприменимо" (напр. ETF без FCF, аналитики
    не покрывают тикер), а не "0" -- иначе такие бумаги ложно проваливали бы или проходили
    гейт. Общий примитив для _score_conservative/_score_revolver, найден при разборе живой
    рекомендации SYF (debt_to_equity=NULL тихо читался как 0.0), тот же анти-паттерн потом
    нашёлся и во free_cash_flow (Claude/16_selection_logic_audit.md, находка D, 2026-08-08).
    """
    if raw_val is None:
        return "N/A", None
    val = float(raw_val)
    return ("PASS" if check_fn(val) else "FAIL"), val


def conservative_fundamental_break_reasons(f: dict) -> list:
    """
    Единый источник правды "фундаментал Консервативной сломан" -- переиспользуется
    PositionExitEvaluator._check_conservative_exit (полный выход) и
    LadderStepWatcher._evaluate_row (2026-08-03: раньше эти две проверки не знали
    друг про друга -- система могла в одном дайджесте одновременно сказать "продавай,
    фундаментал сломан" и "докупай, шаг лесенки готов", см. Claude/BACKLOG.md п.36).
    NULL в поле = "неприменимо" (напр. ETF без ROE/долга), не "0" -- отсутствующий
    показатель просто пропускает свою проверку, не превращается в ложный PASS/FAIL.
    Возвращает список причин (человекочитаемых), пустой список = не сломан.
    """
    reasons = []
    roe_raw = f.get("return_on_equity")
    debt_to_equity_raw = normalize_debt_to_equity(f.get("debt_to_equity"))
    pe_trailing_raw = f.get("pe_trailing")

    if roe_raw is not None and float(roe_raw) < 0.10:
        reasons.append(f"ROE упал до {float(roe_raw) * 100:.1f}% (порог 10%)")
    if debt_to_equity_raw is not None and float(debt_to_equity_raw) > 2.0:
        reasons.append(f"Долг/Капитал вырос до {float(debt_to_equity_raw):.2f} (порог 2.0)")
    if pe_trailing_raw is not None and float(pe_trailing_raw) < 0:
        reasons.append("компания в убытке (P/E отрицательный)")

    return reasons


def _get_fx_rate(db_instance, from_currency: str, to_currency: str) -> float:
    """
    Курс FX БЕЗ учёта currencies.multiplier -- чистое отношение валют. `currency_rates`
    хранит курсы только К USD (USD -- хаб, см. Claude/07_glossary.md), поэтому для
    произвольной целевой валюты считаем в два шага: X -> USD -> Y. Возвращает 1.0
    (с предупреждением в лог), если нужный курс не нашёлся -- не бросает исключение,
    чтобы не ронять экран из-за отсутствующего курса.
    """
    curr_from = str(from_currency).strip().upper()
    curr_to = str(to_currency).strip().upper()
    if curr_from == curr_to:
        return 1.0

    if curr_to == "USD":
        row = db_instance.execute_row(
            "SELECT rate FROM public.currency_rates WHERE from_currency = %s AND to_currency = 'USD' LIMIT 1;",
            (curr_from,)
        )
        if row and row.get("rate"):
            return float(row["rate"])
        logging.warning(f"⚠️ [FX]: Курс {curr_from} -> USD не найден! Используем 1.0")
        return 1.0

    # Хаб через USD: rate(X->USD) / rate(Y->USD)
    row_from = db_instance.execute_row(
        "SELECT rate FROM public.currency_rates WHERE from_currency = %s AND to_currency = 'USD' LIMIT 1;",
        (curr_from,)
    )
    row_to = db_instance.execute_row(
        "SELECT rate FROM public.currency_rates WHERE from_currency = %s AND to_currency = 'USD' LIMIT 1;",
        (curr_to,)
    )
    if row_from and row_from.get("rate") and row_to and row_to.get("rate"):
        return float(row_from["rate"]) / float(row_to["rate"])

    logging.warning(f"⚠️ [FX]: Курс {curr_from} -> {curr_to} (через USD-хаб) не найден! Используем 1.0")
    return 1.0


def convert_currency_amount(db_instance, amount: float, from_currency: str, to_currency: str = "USD") -> float:
    """
    Плоская FX-конвертация БЕЗ multiplier -- для сумм БРОКЕРСКОГО слоя (`listings`,
    `assets`, `orders`, `alerts`): они уже в "настоящих" единицах валюты (см. принцип
    в Claude/07_glossary.md -- multiplier нужен только для канонического слоя `tickers`).
    Используется телом карточки тикера (bot_screens.py) для показа сумм в валюте
    СМОТРЯЩЕГО пользователя, а не валюте листинга/брокера.
    """
    if not amount:
        return 0.0
    rate = _get_fx_rate(db_instance, from_currency, to_currency)
    return float(amount) * rate


def convert_to_base_currency(db_instance, amount: float, from_currency: str, to_currency: str = "USD") -> float:
    """
    💸 УНИВЕРСАЛЬНЫЙ СКВОЗНОЙ КОНВЕРТЕР АНАЛИТИКИ
    Переводит любую сумму из исходной валюты в целевую (по умолчанию USD).
    Учитывает лондонские мультипликаторы пенсов (`currencies.multiplier`) -- только
    для сумм КАНОНИЧЕСКОГО слоя `tickers`/Yahoo (нативные единицы биржи, см.
    Claude/07_glossary.md); для брокерского слоя (`listings`/`assets`/`orders`) —
    см. convert_currency_amount() выше, там мультипликатор не нужен.
    """
    if not amount:
        return 0.0

    curr_from = str(from_currency).strip().upper()
    curr_to = str(to_currency).strip().upper()

    if curr_from == curr_to:
        return float(amount)

    try:
        sql_mult = "SELECT multiplier FROM public.currencies WHERE id = %s LIMIT 1;"
        res_mult = db_instance.execute_query(sql_mult, (curr_from,))
        multiplier = float(res_mult[0]["multiplier"]) if res_mult and res_mult[0].get("multiplier") else 1.0

        rate = _get_fx_rate(db_instance, curr_from, curr_to)

        # 🔥 ФИНАНСОВАЯ МАТЕМАТИКА UPORT: применяем мультипликатор, затем умножаем на курс
        return (float(amount) * multiplier) * rate

    except Exception as err:
        logging.error(f"❌ [Analytics Converter Error]: Сбой конвертации {curr_from} в {curr_to}: {err}")
        return float(amount)


def check_sector_ceiling_breach(sector_exposure_usd: dict, total_capital_usd: float,
                                 sector_target_config: dict, sector: str,
                                 additional_usd: float = 0.0) -> dict:
    """
    Портфельный секторальный потолок (Claude/BACKLOG.md №82) -- один расчёт,
    переиспользуется и реактивно (PortfolioInspector.audit_limits_and_rules,
    факт по всему портфелю прямо сейчас), и проактивно (CashDeploymentAdvisor,
    прогноз с учётом ещё не совершённой покупки). НЕ путать с существующим
    отдельным лимитом на сектор ВНУТРИ одной стратегии (rules_config.portfolio_max_sector_pct)
    -- это независимый, более высокий контур.

    sector_exposure_usd -- {sector: usd} факт по всем активным содержательным
    стратегиям портфеля (PortfolioInspector.get_portfolio_sector_exposure).
    additional_usd -- гипотетическая добавка (например, размер будущей покупки),
    0.0 для чисто реактивной проверки по факту.

    Срабатывание одностороннее -- только превышение потолка, недовес не тревога.
    Сектор, для которого потолок не задан в sector_target_config, пропускается
    (не считается нарушением -- нет ориентира, не наше дело придумывать за
    пользователя).

    Возвращает {} если не пробивает, иначе {"sector", "projected_pct", "limit_pct"}.
    """
    if total_capital_usd <= 0 or not sector_target_config:
        return {}
    limit_pct = sector_target_config.get(sector)
    if limit_pct is None:
        return {}

    current_usd = float(sector_exposure_usd.get(sector) or 0.0)
    projected_usd = current_usd + float(additional_usd)
    projected_pct = (projected_usd / total_capital_usd) * 100.0

    if projected_pct > float(limit_pct):
        return {
            "sector": sector,
            "projected_pct": round(projected_pct, 2),
            "limit_pct": float(limit_pct),
        }
    return {}


class TickerEvaluator:
    """
    🔬 УНИВЕРСАЛЬНЫЙ ОЦЕНЩИК БУМАГ UPORT (TickerEvaluator)
    Анализирует соответствие любой акции трем семейным инвестиционным стратегиям.
    Поддерживает иерархию правил: личные настройки стратегии -> глобальный дефолт СУБД.
    Генерирует лог-карты "ПОЧЕМУ" для интерактивных кнопок Telegram-бота.

    Условия отбора живут ровно в одном месте -- методах _score_revolver/
    _score_conservative/_score_trend (чистые функции без обращения к БД). И
    evaluate_ticker_strategy (один тикер), и screen_universe_for_strategy (весь
    Universe одним запросом) вызывают одни и те же функции -- разница только в
    том, как данные ДОСТАВЛЯЮТСЯ (по одному или пакетом), не в самих правилах.
    """

    # Freedom Broker (short_name='FB') ограничен рынком США -- см.
    # analytics/cash_deployment_advisor.py и BACKLOG.md, Трек C, п.21 (путаница
    # пенсов/фунтов на LSE). CashDeploymentAdvisor переиспользует эту константу.
    US_EXCHANGE_CODES = ("XNYS", "XNAS", "ARCX", "XNMS", "EDGX", "OTCM")

    def __init__(self, db_instance):
        """
        Инициализация оценщика. Кэширует типы данных из словаря аналитических правил.
        """
        self.db = db_instance
        self.global_rules_types = {}
        self._load_rules_vocabulary_types()

    def resolve_us_only(self, portfolio_id: int) -> bool:
        """
        US-only зависит от брокера портфеля -- Freedom Broker (short_name='FB') сегодня
        не размещает неамериканские бумаги. Портфель без реального брокера (broker_id
        IS NULL -- например, бумажный портфель, см. Claude/14_paper_portfolio.md) по
        умолчанию тоже считается US-only, осознанный выбор, не "как повезёт" (решено
        2026-08-03). Единая точка вместо трёх независимых копий одной проверки
        (BACKLOG.md, Трек C, п.53).
        """
        broker_row = self.db.execute_row("""
            SELECT b.short_name FROM public.portfolios p
            JOIN public.brokers b ON p.broker_id = b.id
            WHERE p.id = %s;
        """, (portfolio_id,))
        short_name = (broker_row or {}).get("short_name")
        if short_name is None:
            return True
        return short_name == "FB"

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

    # -------------------------------------------------------------------------
    # ЧИСТЫЕ ФУНКЦИИ ОТБОРА (без обращения к БД) -- ЕДИНСТВЕННЫЙ источник правды
    # для условий каждой стратегии. Принимают факты ОДНОГО тикера (словарь,
    # неважно, пришёл ли он из выборки на один тикер или пакетом на весь Universe)
    # + rules_config стратегии + days_to_report. Возвращают metrics/is_compatible/
    # ranking_value -- ровно то, что раньше строилось инлайн внутри
    # evaluate_ticker_strategy.
    # -------------------------------------------------------------------------

    def _score_revolver(self, f: dict, rules_config: dict, days_to_report: int, us_only: bool = False) -> dict:
        limit_turnover1 = self._get_rule_value(rules_config, "idea_min_turnover_usd", 500000000.0)
        limit_rsi1 = self._get_rule_value(rules_config, "idea_rsi_oversold_num", 45.0)
        limit_buffer1 = self._get_rule_value(rules_config, "idea_report_buffer_days", 5)
        require_fcf1 = self._get_rule_value(rules_config, "idea_require_positive_fflow_bool", True)

        turnover = float(f.get("daily_turnover_usd") or 0.0)
        rsi = float(f.get("signal_rsi") or 0.0)
        macd_val = str(f.get("signal_macd") or "").strip()
        try:
            macd_numeric = float(macd_val) if macd_val else 0.0
        except ValueError:
            macd_numeric = 0.0
        rec_mean = float(f.get("recommendation_mean") or 0.0)
        fcf_status, fcf_val = na_or_check(f.get("free_cash_flow"), lambda v: v > 0)
        curr_price = float(f.get("current_price") or 0.0)

        # target_mean_price NULL (аналитики не покрывают тикер) раньше читался как $0 --
        # ранжирование получало -100% ("аналитики ждут падения до нуля"), хотя факт в том,
        # что мнения просто нет. Нейтральное значение (0%), не исключение из пула --
        # остальной фундаментал тикера остаётся валидным основанием для идеи
        # (Claude/16_selection_logic_audit.md, находка Д/E, 2026-08-08).
        tgt_price_raw = f.get("target_mean_price")
        if tgt_price_raw is None:
            upside_pct = 0.0
        else:
            tgt_price = float(tgt_price_raw)
            upside_pct = ((tgt_price - curr_price) / curr_price * 100.0) if curr_price > 0 else 0.0

        # Вход и выход Револьверной раньше не сверялись друг с другом -- RSI<45 не отличает
        # "только начало падать" от "уже N дней подряд подтверждённо падает без разворота",
        # а именно это второе условие -- то же самое, по которому _check_revolver_exit решает
        # "ставка не отыгрывается, сдавайся раньше срока". Итог: система могла купить бумагу
        # в момент, когда та уже удовлетворяла своему же условию немедленной продажи (живой
        # случай MU в «ПБум», 2026-08-03). Порог -- тот же TREND_REVERSAL_CONFIRM_DAYS, что и
        # на выходе, для согласованности.
        streak = f.get("signal_ema20_streak_days")
        momentum_broken = streak is not None and int(streak) <= -settings.TREND_REVERSAL_CONFIRM_DAYS

        m1 = {
            "idea_min_turnover_usd": {"status": "PASS" if turnover >= limit_turnover1 else "FAIL", "fact": round(turnover, 2), "limit": limit_turnover1},
            "idea_rsi_oversold_num": {"status": "PASS" if rsi < limit_rsi1 else "FAIL", "fact": rsi, "limit": limit_rsi1},
            "speculative_catalyst": {"status": "PASS" if (macd_numeric > 0 or (0 < rec_mean <= 2.0)) else "FAIL", "fact": f"MACD: {macd_val}, Rec: {rec_mean}", "limit": "MACD > 0 ИЛИ Rec <= 2.0"},
            "idea_require_positive_fflow_bool": {"status": "PASS" if not require_fcf1 else fcf_status, "fact": round(fcf_val, 2) if fcf_val is not None else None, "limit": "FCF > 0"},
            "trend_not_confirmed_broken": {"status": "FAIL" if momentum_broken else "PASS", "fact": streak, "limit": f"> -{settings.TREND_REVERSAL_CONFIRM_DAYS}"},
            "idea_report_buffer_days": {"status": "PASS" if days_to_report >= limit_buffer1 else "WARNING", "fact": days_to_report, "limit": limit_buffer1}
        }
        # N/A (показатель неприменим) не считается провалом, в отличие от FAIL -- та же
        # логика, что и в _score_conservative.
        is_compat1 = all(x["status"] in ("PASS", "N/A") for k, x in m1.items() if k != "idea_report_buffer_days")
        return {"metrics": m1, "is_compatible": is_compat1, "ranking_value": round(upside_pct, 2)}

    def _score_conservative(self, f: dict, rules_config: dict, days_to_report: int, us_only: bool = False) -> dict:
        limit_turnover2 = self._get_rule_value(rules_config, "idea_min_turnover_usd", 100000000.0)
        limit_buffer2 = self._get_rule_value(rules_config, "idea_report_buffer_days", 5)
        require_fcf2 = self._get_rule_value(rules_config, "idea_require_positive_fflow_bool", True)
        limit_max_volatility2 = self._get_rule_value(rules_config, "idea_max_daily_volatility_pct", 5.0)

        turnover = float(f.get("daily_turnover_usd") or 0.0)
        price_to_sma200 = float(f.get("signal_price_to_sma200_pct") or 0.0)
        rsi = float(f.get("signal_rsi") or 0.0)

        # "Мировой лидер" -- членство в курируемом индексе (tickers.provenance), не биржа
        # напрямую: биржевой фильтр (XNGS/XNYS/XNAS) не различал реального лидера от любой
        # мелкой бумаги, торгующейся в США, см. Claude/12_investment_goal_and_mechanisms_roadmap.md
        # (2026-08-02). MS_SP500 -- всегда; MS_FTSE100 (Лондон, exchange_mic='XLON') -- только
        # если брокер портфеля умеет с ним нормально работать (us_only=False, сегодня T212;
        # Freedom Broker плохо работает с английскими бумагами, us_only=True для FB).
        provenance = f.get("provenance") or {}
        is_leader = "MS_SP500" in provenance or (not us_only and "MS_FTSE100" in provenance)

        # NULL в фундаментальных полях = "неприменимо" (напр. commodity ETF без ROE/долга),
        # а не "0" -- иначе такие бумаги ложно проходили бы отбор. Тот же анти-паттерн уже
        # чинили в PositionExitEvaluator для выхода из позиции, здесь -- для входа
        # (найдено при разборе живой рекомендации SYF, где debt_to_equity=NULL раньше
        # тихо читался как 0.0 и автоматически проходил порог < 1.5).
        roe_status, roe_val = na_or_check(f.get("return_on_equity"), lambda v: v > 0.15)
        debt_status, debt_val = na_or_check(normalize_debt_to_equity(f.get("debt_to_equity")), lambda v: v < 1.5)
        cagr_status, cagr_val = na_or_check(f.get("revenue_cagr_3y"), lambda v: v > 0.05)
        growth_status, growth_val = na_or_check(f.get("revenue_growth"), lambda v: v > 0.00)
        pe_status, pe_val = na_or_check(f.get("pe_trailing"), lambda v: v > 0)
        vol_status, vol_val = na_or_check(f.get("signal_daily_volatility_pct"), lambda v: v <= limit_max_volatility2)
        fcf_status, fcf_val = na_or_check(f.get("free_cash_flow"), lambda v: v > 0)

        # Буферная зона дисконта (Claude/16_selection_logic_audit.md, находка Q,
        # 2026-08-08): порог "ниже SMA200" нормализован по волатильности бумаги --
        # < K × daily_volatility_pct вместо фиксированного < 0.00%, чтобы не отсекать
        # качественных лидеров, которые торгуются у своей SMA200 в пределах
        # собственного дневного шума. Если волатильность NULL (нечем нормализовать) --
        # откат к прежнему строгому < 0.00%, а не пропуск гейта.
        discount_buffer_limit = settings.CONSERVATIVE_DISCOUNT_BUFFER_MULTIPLIER * vol_val if vol_val is not None else 0.00

        # Строка, не list(...) -- квадратные скобки сырого Python-списка ('[...]') ломают
        # Telegram Markdown (парсер читает их как начало ссылки без пары "(url)" и роняет
        # edit_text с TelegramBadRequest, которую process_view_idea_reason тихо глотает --
        # экран просто не появлялся, без следа в логе. Живой баг, найден 2026-08-03.
        provenance_fact = ", ".join(provenance.keys()) or "—"

        m2 = {
            "world_leader_index": {"status": "PASS" if is_leader else "FAIL", "fact": provenance_fact, "limit": "MS_SP500" if us_only else "MS_SP500 или MS_FTSE100"},
            "idea_min_turnover_usd": {"status": "PASS" if turnover >= limit_turnover2 else "FAIL", "fact": round(turnover, 2), "limit": limit_turnover2},
            "return_on_equity": {"status": roe_status, "fact": round(roe_val, 4) if roe_val is not None else None, "limit": "> 0.15"},
            "debt_to_equity": {"status": debt_status, "fact": round(debt_val, 4) if debt_val is not None else None, "limit": "< 1.5"},
            "revenue_cagr_3y": {"status": cagr_status, "fact": round(cagr_val, 4) if cagr_val is not None else None, "limit": "> 0.05"},
            "revenue_growth": {"status": growth_status, "fact": round(growth_val, 4) if growth_val is not None else None, "limit": "> 0.00"},
            "pe_trailing": {"status": pe_status, "fact": round(pe_val, 2) if pe_val is not None else None, "limit": "> 0"},
            "idea_require_positive_fflow_bool": {"status": "PASS" if not require_fcf2 else fcf_status, "fact": round(fcf_val, 2) if fcf_val is not None else None, "limit": "FCF > 0"},
            "signal_daily_volatility_pct": {"status": vol_status, "fact": round(vol_val, 4) if vol_val is not None else None, "limit": f"<= {limit_max_volatility2}"},
            "signal_price_to_sma200_pct": {"status": "PASS" if price_to_sma200 < discount_buffer_limit else "FAIL", "fact": price_to_sma200, "limit": f"< {round(discount_buffer_limit, 4)}"},
            "signal_rsi": {"status": "PASS" if rsi > 35 else "FAIL", "fact": rsi, "limit": "> 35"},
            "idea_report_buffer_days": {"status": "PASS" if days_to_report >= limit_buffer2 else "WARNING", "fact": days_to_report, "limit": limit_buffer2}
        }
        # N/A (показатель неприменим) не считается провалом, в отличие от FAIL
        is_compat2 = all(x["status"] in ("PASS", "N/A") for k, x in m2.items() if k != "idea_report_buffer_days")
        rank2 = (roe_val / debt_val) if (roe_val is not None and debt_val) else (roe_val or 0.0)
        return {"metrics": m2, "is_compatible": is_compat2, "ranking_value": round(rank2, 4)}

    def _score_trend(self, f: dict, rules_config: dict, days_to_report: int, us_only: bool = False) -> dict:
        limit_turnover3 = self._get_rule_value(rules_config, "idea_min_turnover_usd", 100000000.0)
        limit_buffer3 = self._get_rule_value(rules_config, "idea_report_buffer_days", 5)

        turnover = float(f.get("daily_turnover_usd") or 0.0)
        price_to_sma200 = float(f.get("signal_price_to_sma200_pct") or 0.0)
        rsi = float(f.get("signal_rsi") or 0.0)
        macd_val = str(f.get("signal_macd") or "").strip()
        try:
            macd_numeric = float(macd_val) if macd_val else 0.0
        except ValueError:
            macd_numeric = 0.0

        ema20 = float(f.get("signal_ema_20") or 0.0)
        sma50 = float(f.get("signal_sma_50") or 0.0)
        sma100 = float(f.get("signal_sma_100") or 0.0)
        sma200 = float(f.get("signal_sma_200") or 0.0)
        fan_is_valid = (ema20 > sma50) and (sma50 > sma100) and (sma100 > sma200)

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

        # Risk-adjusted momentum (Claude/16_selection_logic_audit.md, находка Б,
        # 2026-08-08): сырой % отрыва от SMA200 механически выносит наверх самые
        # экстремальные движения (DELL +112%), не обязательно самые качественные
        # тренды -- крупные компании физически не могут дать такой же отрыв, их
        # размер сглаживает движение. Нормализация по волатильности бумаги (тот же
        # переиспользуемый примитив, что и K-множители в settings.py) разводит
        # "устойчивый тренд" и "случайный резкий скачок" корректно. Волатильность
        # в ЗНАМЕНАТЕЛЕ (не множителем, как у остальных потребителей примитива) --
        # NULL/0 откатывает к прежнему сырому % вместо деления на ноль/взрыва
        # результата (последнее уже поймано на живых данных LSE, BACKLOG.md №94 --
        # там же зафиксировано, почему не нормализуется, пока не подключён T212).
        vol = f.get("signal_daily_volatility_pct")
        ranking_value = (price_to_sma200 / vol) if vol else price_to_sma200

        return {"metrics": m3, "is_compatible": is_compat3, "ranking_value": ranking_value}

    def _get_strategy_scorers(self) -> dict:
        """Карта system_key -> (человекочитаемое имя, функция скоринга)."""
        return {
            "REVOLVER": ("Револьверная стратегия", self._score_revolver),
            "CONSERVATIVE_ACCUMULATION": ("Консервативное накопление", self._score_conservative),
            "TREND_FOLLOWING": ("Стратегия следования за трендом", self._score_trend),
        }

    TICKER_FACTS_SQL_COLUMNS = """
        id, symbol, company_name, sector, industry, exchange_mic, provenance,
        daily_turnover_usd, return_on_equity, debt_to_equity,
        revenue_cagr_3y, revenue_growth, pe_trailing, dividend_yield, free_cash_flow,
        current_price, target_mean_price, recommendation_mean, signal_next_report_date,
        signal_rsi, signal_macd, signal_ema_20, signal_sma_50, signal_sma_100, signal_sma_200,
        signal_price_to_sma200_pct, signal_daily_volatility_pct, signal_ema20_streak_days
    """

    def evaluate_ticker_strategy(self, ticker_id: int, target_portfolio_id: int = 2) -> dict:
        """
        🌐 ГЛАВНАЯ СКВОЗНАЯ ТОЧКА ВХОДА ЭВАЛЮАТОРА UPORT (один тикер)
        Сводит воедино все расчеты по трем стратегиям и формирует паспорт актива.
        """
        # TICKER_FACTS_SQL_COLUMNS -- фиксированный список колонок константой класса
        # (не из пользовательского ввода), остаётся f-строкой (Claude/BACKLOG.md №81).
        sql_ticker = f"SELECT {self.TICKER_FACTS_SQL_COLUMNS} FROM public.tickers WHERE id = %s LIMIT 1;"
        ticker_res = self.db.execute_query(sql_ticker, (ticker_id,))
        if not ticker_res:
            return {"error": f"Тикер с ID={ticker_id} не найден в СУБД."}

        f = ticker_res[0] if isinstance(ticker_res, list) and len(ticker_res) > 0 else (ticker_res if isinstance(ticker_res, dict) else {})
        if not f or "id" not in f:
            return {"error": f"Ошибка распаковки структуры данных тикера ID={ticker_id}"}

        us_only = self.resolve_us_only(target_portfolio_id)

        # Классифицируем по system_key, т.к. strategies.id — сквозной автоинкремент по
        # всей БД и не привязан к порядковому номеру стратегии внутри портфеля.
        # is_screening_active = false -- пассивная стратегия (см. Claude/BACKLOG.md п.9,
        # 2026-07-31), не подбираем под неё новых кандидатов, пока не включена.
        sql_strat = """
            SELECT s.id, s.rules_config, st.system_key
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = %s AND s.is_active = true AND s.is_screening_active = true;
        """
        strat_rows = self.db.execute_query(sql_strat, (target_portfolio_id,)) or []
        clean_strat_rows = strat_rows if isinstance(strat_rows, list) else [strat_rows]

        strategy_configs = {}
        strategy_ids = {}
        for s in clean_strat_rows:
            if s and s.get("system_key"):
                key = s["system_key"]
                strategy_configs[key] = s["rules_config"] or {}
                strategy_ids[key] = int(s["id"])

        evaluation_report = {
            "ticker_id": int(f["id"]),
            "symbol": f.get("symbol"),
            "company_name": f.get("company_name"),
            "compatible_strategies": [],
            "tax_and_limit_alerts": [],
            "explain_map": {}
        }

        div_yield_pct = float(f.get("dividend_yield") or 0.0)  # В СУБД лежат чистые проценты
        days_to_report = self._calculate_days_to_report(f.get("signal_next_report_date"))

        for key, (strat_name, score_fn) in self._get_strategy_scorers().items():
            if key not in strategy_configs:
                continue
            sid = strategy_ids[key]
            conf = strategy_configs[key]
            result = score_fn(f, conf, days_to_report, us_only=us_only)

            evaluation_report["explain_map"][sid] = {
                "strategy_name": strat_name,
                "is_compatible_technically": result["is_compatible"],
                "metrics": result["metrics"],
                "ranking_value": result["ranking_value"]
            }
            self._apply_tax_and_warning_filters(evaluation_report, sid, result["is_compatible"], config=conf, div_yield_pct=div_yield_pct, m_report=result["metrics"])

        return evaluation_report

    def screen_universe_for_strategy(self, strategy_id: int, exclude_ticker_ids: set = None, us_only: bool = False) -> list:
        """
        🚀 БЫСТРЫЙ СКАН ВСЕГО UNIVERSE ПОД ОДНУ СТРАТЕГИЮ
        Один SQL-запрос на ВСЕ тикеры (вместо N+1 отдельных вызовов
        evaluate_ticker_strategy) + один запрос конфига стратегии, дальше --
        та же самая функция скоринга (_score_revolver/_score_conservative/
        _score_trend), но в цикле по уже загруженным в память строкам. Никаких
        обращений к БД внутри цикла -- отсюда и ускорение с ~60-80 сек до долей
        секунды на ~945 тикеров.

        Возвращает список ТОЛЬКО совместимых тикеров, отсортированный по
        ranking_value по убыванию: [{"ticker_id", "symbol", "ranking_value",
        "metrics"}, ...].
        """
        strat_row = self.db.execute_row("""
            SELECT s.rules_config, st.system_key
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.id = %s;
        """, (strategy_id,))
        if not strat_row or not strat_row.get("system_key"):
            return []

        scorers = self._get_strategy_scorers()
        scorer_entry = scorers.get(strat_row["system_key"])
        if not scorer_entry:
            return []  # CASH_RESERVE/UNALLOCATED -- у них нет правила входа
        _, score_fn = scorer_entry

        rules_config = strat_row.get("rules_config") or {}
        exclude_ticker_ids = exclude_ticker_ids or set()

        # US_EXCHANGE_CODES -- фиксированный кортеж-константа класса (не из пользовательского
        # ввода), остаётся f-строкой. exclude_ticker_ids -- динамический набор id, IN-список
        # не работает через JSON-параметризацию (см. Claude/BACKLOG.md №81) -- ANY(%s).
        sql = f"SELECT {self.TICKER_FACTS_SQL_COLUMNS} FROM public.tickers"
        conditions = []
        params = []
        if us_only:
            codes = "', '".join(self.US_EXCHANGE_CODES)
            conditions.append(f"exchange_mic IN ('{codes}')")
        if exclude_ticker_ids:
            conditions.append("NOT (id = ANY(%s))")
            params.append([int(t) for t in exclude_ticker_ids])
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += ";"

        rows = self.db.execute_query(sql, tuple(params)) or []
        rows = rows if isinstance(rows, list) else [rows]

        results = []
        for f in rows:
            if not f or "id" not in f:
                continue
            days_to_report = self._calculate_days_to_report(f.get("signal_next_report_date"))
            scored = score_fn(f, rules_config, days_to_report, us_only=us_only)
            if scored["is_compatible"]:
                results.append({
                    "ticker_id": int(f["id"]),
                    "symbol": f.get("symbol"),
                    "sector": f.get("sector") or "Unknown Sector",
                    "ranking_value": scored["ranking_value"],
                    "metrics": scored["metrics"],
                })

        results.sort(key=lambda r: r["ranking_value"], reverse=True)
        return results

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