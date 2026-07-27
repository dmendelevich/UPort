import logging

def generate_portfolio_passport(portfolio_id: int, db_instance) -> dict:
    """
    Рассчитывает средневзвешенные фундаментальные показатели портфеля.
    Сверяет фактические метрики со стратегическими и налоговыми лимитами (IPS).
    """
    # 1. ЗАГРУЖАЕМ ЦЕЛЕВЫЕ ЛИМИТЫ (IPS) ИЗ БАЗЫ ДАННЫХ
    p_sql = f"""
        SELECT p.name AS portfolio_name, u.name AS owner_name, p.strategy_type, p.risk_profile,
               p.min_cash_reserve_pct, p.max_ticker_weight_pct, p.max_portfolio_div_yield_pct, p.target_return_pct
        FROM public.portfolios p
        JOIN public.users u ON p.owner_id = u.id
        WHERE p.id = {portfolio_id};
    """

    p_res = db_instance.execute_query(p_sql)
    if not p_res:
        logging.error(f"❌ Портфель с ID {portfolio_id} не найден при аудите.")
        return {}

    # Безопасное извлечение одиночной строки из ответа шлюза
    if isinstance(p_res, list) and len(p_res) > 0:
        limits = p_res[0]
    elif isinstance(p_res, dict):
        limits = p_res
    else:
        limits = {}

    # 🔥 ФИКС ИНИЦИАЛИЗАЦИИ: Объявляем лимиты НАВЕРХУ для защиты от UnboundLocalError
    min_cash_limit = float(limits.get("min_cash_reserve_pct", 10.0))
    max_w_limit = float(limits.get("max_ticker_weight_pct", 15.0))
    max_div_limit = float(limits.get("max_portfolio_div_yield_pct", 2.0))

    # 2. СБОР ФАКТИЧЕСКИХ ОСТАТКОВ КЭША К ДОЛЛАРУ США
    cash_sql = f"""
        SELECT acc.cash_available, COALESCE(r.rate, 1.0) as to_usd_rate
        FROM public.accounts acc
        LEFT JOIN public.currency_rates r ON r.from_currency = acc.currency_id AND r.to_currency = 'USD'
        WHERE acc.portfolio_id = {portfolio_id};
    """
    cash_res = db_instance.execute_query(cash_sql)
    if not isinstance(cash_res, list):
        cash_res = [cash_res] if cash_res else []

    total_cash_usd = 0.0
    for c in cash_res:
        total_cash_usd += float(c['cash_available'] or 0) * float(c['to_usd_rate'])

    # 3. СБОР ФАКТИЧЕСКИХ ЦЕННЫХ БУМАГ (ЧЕРЕЗ LISTINGS)
    assets_sql = f"""
        SELECT
            l.broker_symbol AS full_ticker, t.company_name, a.quantity, l.last_price,
            t.pe_trailing, t.pe_forward, t.peg_ratio, t.price_to_sales, t.price_to_book, t.ev_to_ebitda,
            t.debt_to_equity, t.current_ratio, t.quick_ratio,
            t.profit_margin, t.operating_margin, t.return_on_equity, t.return_on_assets,
            t.dividend_yield, t.payout_ratio, t.free_cash_flow, t.max_ticker_div_yield_pct,
            COALESCE(r.rate, 1.0) as asset_to_usd_rate
        FROM public.assets a
        JOIN public.listings l ON a.listing_id = l.id
        JOIN public.tickers t ON l.ticker_id = t.id
        LEFT JOIN public.currency_rates r ON r.from_currency = l.currency_id AND r.to_currency = 'USD'
        WHERE a.portfolio_id = {portfolio_id} AND a.quantity > 0;
    """
    assets_res = db_instance.execute_query(assets_sql)
    if not isinstance(assets_res, list):
        assets_res = [assets_res] if assets_res else []

    total_assets_usd = 0.0
    processed_shares = []

    for row in assets_res:
        qty = float(row['quantity'])
        price_raw = float(row['last_price'] or 0)
        to_usd = float(row['asset_to_usd_rate'])
        
        market_value_usd = qty * price_raw * to_usd
        total_assets_usd += market_value_usd
        
        processed_shares.append({
            "row_data": row,
            "market_value_usd": market_value_usd,
            "weight_pct": 0.0
        })

    total_portfolio_value_usd = total_assets_usd + total_cash_usd
    fact_cash_pct = (total_cash_usd / total_portfolio_value_usd * 100) if total_portfolio_value_usd > 0 else 0.0

    # 4. МАТЕМАТИЧЕСКИЙ РАСЧЕТ СРЕДНЕВЗВЕШЕННЫХ ПАРАМЕТРОВ ПО ВЕСАМ
    weighted_pe_trailing = 0.0
    weighted_pe_forward = 0.0
    weighted_peg = 0.0
    weighted_ps = 0.0
    weighted_pb = 0.0
    weighted_ev_ebitda = 0.0
    weighted_debt_equity = 0.0
    weighted_current_ratio = 0.0
    weighted_profit_margin = 0.0
    weighted_roe = 0.0
    weighted_div_yield = 0.0
    total_fcf_millions = 0.0
    
    weights_denominators = {k: 0.0 for k in ['pe_t', 'pe_f', 'peg', 'ps', 'pb', 'ev', 'debt', 'curr', 'margin', 'roe', 'div']}
    violations = []
    ticker_warnings = []

    for share in processed_shares:
        row = share["row_data"]
        mv_usd = share["market_value_usd"]
        weight = (mv_usd / total_portfolio_value_usd * 100) if total_portfolio_value_usd > 0 else 0.0
        share["weight_pct"] = weight

        # Концентрация актива в портфеле
        if weight > max_w_limit:
            violations.append(f"⚠️ Перекос: `{row['full_ticker']}` занимает {weight:.1f}% портфеля (Лимит < {max_w_limit}%)")

        # Индивидуальный налоговый лимит акции
        if row['dividend_yield'] is not None:
            fact_ticker_div = float(row['dividend_yield'])
            max_ticker_div_limit = float(row['max_ticker_div_yield_pct'] or 2.5)
            if fact_ticker_div > max_ticker_div_limit:
                ticker_warnings.append(f"🛑 Налоговый риск: `{row['full_ticker']}` платит {fact_ticker_div:.2f}% див. (Твой лимит для акций < {max_ticker_div_limit}%)")

        def add_weighted(field_name, denom_key, target_var):
            if row[field_name] is not None:
                val = float(row[field_name])
                weights_denominators[denom_key] += weight
                return target_var + (val * weight)
            return target_var

        weighted_pe_trailing = add_weighted('pe_trailing', 'pe_t', weighted_pe_trailing)
        weighted_pe_forward = add_weighted('pe_forward', 'pe_f', weighted_pe_forward)
        weighted_peg = add_weighted('peg_ratio', 'peg', weighted_peg)
        weighted_ps = add_weighted('price_to_sales', 'ps', weighted_ps)
        weighted_pb = add_weighted('price_to_book', 'pb', weighted_pb)
        weighted_ev_ebitda = add_weighted('ev_to_ebitda', 'ev', weighted_ev_ebitda)
        weighted_debt_equity = add_weighted('debt_to_equity', 'debt', weighted_debt_equity)
        weighted_current_ratio = add_weighted('current_ratio', 'curr', weighted_current_ratio)
        weighted_profit_margin = add_weighted('profit_margin', 'margin', weighted_profit_margin)
        weighted_roe = add_weighted('return_on_equity', 'roe', weighted_roe)
        weighted_div_yield = add_weighted('dividend_yield', 'div', weighted_div_yield)

        if row['free_cash_flow'] is not None:
            total_fcf_millions += (float(row['free_cash_flow']) / 1_000_000)

    def normalize_weighted(weighted_val, denom_key):
        denom = weights_denominators[denom_key]
        return (weighted_val / denom) if denom > 0 else 0.0

    final_pe_trailing = normalize_weighted(weighted_pe_trailing, 'pe_t')
    final_pe_forward = normalize_weighted(weighted_pe_forward, 'pe_f')
    final_peg = normalize_weighted(weighted_peg, 'peg')
    final_ps = normalize_weighted(weighted_ps, 'ps')
    final_pb = normalize_weighted(weighted_pb, 'pb')
    final_ev_ebitda = normalize_weighted(weighted_ev_ebitda, 'ev')
    final_debt_equity = normalize_weighted(weighted_debt_equity, 'debt')
    final_current_ratio = normalize_weighted(weighted_current_ratio, 'curr')
    final_profit_margin = normalize_weighted(weighted_profit_margin, 'margin')
    final_roe = normalize_weighted(weighted_roe, 'roe')
    final_div_yield = normalize_weighted(weighted_div_yield, 'div')

    # 5. СТРАТЕГИЧЕСКИЙ АУДИТ ЛИМИТОВ ПОРТФЕЛЯ
    if fact_cash_pct < min_cash_limit:
        violations.append(f"⚠️ Недостаток кэша: Резерв {fact_cash_pct:.1f}% ниже лимита стратегии (> {min_cash_limit}%)")

    if final_div_yield > max_div_limit:
        violations.append(f"🛑 Налоговая перегрузка: Ср. дивиденды портфеля {final_div_yield:.2f}% превышают лимит (< {max_div_limit}%)")

    return {
        "portfolio_id": portfolio_id,
        "meta": {
            "name": limits.get("portfolio_name", f"Портфель #{portfolio_id}"), 
            "owner": limits.get("owner_name", "Unknown"),
            "strategy": limits.get("strategy_type", "Not Set"), 
            "risk": limits.get("risk_profile", "Moderate"),
            "total_value_usd": total_portfolio_value_usd, 
            "cash_usd": total_cash_usd, 
            "cash_pct": fact_cash_pct, 
            "assets_usd": total_assets_usd
        },
        "averages": {
            "pe_trailing": final_pe_trailing, "pe_forward": final_pe_forward, "peg_ratio": final_peg,
            "price_to_sales": final_ps, "price_to_book": final_pb, "ev_to_ebitda": final_ev_ebitda,
            "debt_to_equity": final_debt_equity, "current_ratio": final_current_ratio, "profit_margin": final_profit_margin,
            "return_on_equity": final_roe, "dividend_yield": final_div_yield, "free_cash_flow_m": total_fcf_millions
        },
        "limits": {
            "min_cash_pct": min_cash_limit, "max_ticker_weight_pct": max_w_limit, "max_portfolio_div_pct": max_div_limit, "target_return_pct": float(limits.get("target_return_pct", 10))
        },
        "violations": violations,
        "ticker_warnings": ticker_warnings
    }


def audit_ticker_for_portfolio(full_ticker: str, portfolio_id: int, db_instance) -> list:
    """
    Проводит кросс-аудит одной выбранной бумаги на соответствие рамкам конкретного портфеля.
    Если portfolio_id == 0 (Сводный портфель) — аудит принудительно отключается.
    """
    if portfolio_id == 0:
        return []

    warnings = []
    broker_symbol_clean = full_ticker.strip().upper()

    # 1. Запрашиваем параметры тикера через связь таблицы listings с глобальным tickers
    t_sql = f"""
        SELECT t.id, t.dividend_yield, t.max_ticker_div_yield_pct 
        FROM public.listings l
        JOIN public.tickers t ON l.ticker_id = t.id
        WHERE l.broker_symbol = '{broker_symbol_clean}' LIMIT 1;
    """
    t_res = db_instance.execute_query(t_sql)
    if not t_res:
        return []
    
    if isinstance(t_res, list) and len(t_res) > 0:
        ticker_data = t_res[0]
    elif isinstance(t_res, dict):
        ticker_data = t_res
    else:
        return []

    t_id = ticker_data['id']
    ticker_div = float(ticker_data['dividend_yield'] or 0)
    max_ticker_div_limit = float(ticker_data['max_ticker_div_yield_pct'] or 2.50)

    if ticker_data['dividend_yield'] is not None and ticker_div > max_ticker_div_limit:
        warnings.append(f"🛑 Налоговый риск акции: Дивиденды актива ({ticker_div:.2f}%) выше твоего личного лимита для этой бумаги (< {max_ticker_div_limit}%)")

    # 2. Запрашиваем лимиты портфеля
    p_sql = f"SELECT max_ticker_weight_pct, max_portfolio_div_yield_pct FROM public.portfolios WHERE id = {portfolio_id};"
    p_res = db_instance.execute_query(p_sql)
    if not p_res:
        return warnings
    
    if isinstance(p_res, list) and len(p_res) > 0:
        p_limits = p_res[0]
    elif isinstance(p_res, dict):
        p_limits = p_res
    else:
        return warnings

    max_portfolio_div_limit = float(p_limits.get('max_portfolio_div_yield_pct', 2.00))
    max_weight_limit = float(p_limits.get('max_ticker_weight_pct', 15.00))

    if ticker_data['dividend_yield'] is not None and ticker_div > max_portfolio_div_limit:
        warnings.append(f"🛑 Налоговый риск портфеля: Дивиденды актива ({ticker_div:.2f}%) превышают допустимый порог этого счета (< {max_portfolio_div_limit}%)")

    # 3. Считаем общий объем капитала портфеля в USD по академической схеме v3.4
    cash_sql = f"""
        SELECT acc.cash_available, COALESCE(r.rate, 1.0) as to_usd_rate
        FROM public.accounts acc
        LEFT JOIN public.currency_rates r ON r.from_currency = acc.currency_id AND r.to_currency = 'USD'
        WHERE acc.portfolio_id = {portfolio_id};
    """
    cash_res = db_instance.execute_query(cash_sql)
    if not isinstance(cash_res, list):
        cash_res = [cash_res] if cash_res else []
    
    portfolio_total_usd = 0.0
    for c in cash_res:
        portfolio_total_usd += float(c['cash_available'] or 0) * float(c['to_usd_rate'])

    assets_sql = f"""
        SELECT a.quantity, l.last_price, COALESCE(r.rate, 1.0) as asset_to_usd_rate, l.broker_symbol AS full_ticker
        FROM public.assets a
        JOIN public.listings l ON a.listing_id = l.id
        LEFT JOIN public.currency_rates r ON r.from_currency = l.currency_id AND r.to_currency = 'USD'
        WHERE a.portfolio_id = {portfolio_id} AND a.quantity > 0;
    """
    assets_res = db_instance.execute_query(assets_sql)
    if not isinstance(assets_res, list):
        assets_res = [assets_res] if assets_res else []

    target_asset_value_usd = 0.0
    for asset in assets_res:
        asset_value_usd = float(asset['quantity']) * float(asset['last_price'] or 0) * float(asset['asset_to_usd_rate'])
        portfolio_total_usd += asset_value_usd
        if asset['full_ticker'] == broker_symbol_clean:
            target_asset_value_usd = asset_value_usd

    fact_weight = (target_asset_value_usd / portfolio_total_usd * 100) if portfolio_total_usd > 0 else 0.0

    if fact_weight > max_weight_limit:
        warnings.append(f"⚠️ Превышение лимита веса: Ассет занимает {fact_weight:.1f}% объема этого портфеля (Максимум по стратегии < {max_weight_limit}%)")

    return warnings
