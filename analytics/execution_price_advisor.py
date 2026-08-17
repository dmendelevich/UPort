import logging
import math

import yfinance as yf

import settings
from analytics.quote_snapshot_utils import compute_price_move


def suggest_execution_terms(db_instance, listing_id: int, action: str) -> dict:
    """
    Сигнал (A), см. Claude/19_price_move_protection_design.md -- не МОМЕНТ времени
    (система для настоящих портфелей никогда не исполняет сама, только советует), а
    МОМЕНТ ЦЕНЫ: стоит ли рекомендовать `market`, или разумнее `limit` у конкретного
    уровня, чтобы не покупать на случайном коротком всплеске / не продавать в случайный
    короткий провал. Считается заново при каждом вызове (не кэшируется) -- см. правило
    "перезапросить пропущенное" из обсуждения принципов.

    Асимметрия (согласована с пользователем): BUY тревожен рост цены ПРЯМО СЕЙЧАС (не
    гнаться), SELL тревожен провал ПРЯМО СЕЙЧАС (не продавать в случайную слабость).

    Два независимых режима, выбираются по `marketState` (yfinance):
    - **VWAP-режим** (рынок в REGULAR, сессия открыта дольше EXECUTION_TIMING_SESSION_WARMUP_MINUTES) --
      сравниваем текущую цену с сессионным VWAP (volume-weighted, `yfinance` 1-минутные
      бары с объёмом). Задержка `getHloc` брокера сделала этот путь непригодным
      (проверено эмпирически, отставание часы, не минуты) -- `yfinance` intraday
      подтверждена свежей (<1 мин отставания, проверено на нескольких тикерах).
    - **Статистический режим** (иначе -- рынок закрыт/pre/post/только что открылся) --
      тот же примитив, что уже использует `PriceMoveWatcher` (`compute_price_move` +
      нормировка на волатильность, ВРЕМЕННАЯ составляющая есть -- отвечаем на тот же
      вопрос "насколько велико движение за ЭТО окно", не на вопрос сигнала (B)
      "насколько ненормально независимо от срока держания"). Если рынок закрыт, брокер
      не обновляет `listings.last_price` -- движение само по себе выйдет ~0%, сигнал
      естественно откатится на `market`, специальный случай не нужен.

    Возвращает {"mode": "market"|"limit", "price": float|None, "reference": "vwap"|"recent"|None}.
    """
    action = str(action).upper().strip()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"action должен быть 'BUY' или 'SELL', получено: {action!r}")

    row = db_instance.execute_row("""
        SELECT listing_id, symbol, listing_last_price, signal_daily_volatility_pct, ticker_name_map
        FROM public.v_listings_tickers
        WHERE listing_id = %s;
    """, (listing_id,))
    if not row:
        return {"mode": "market", "price": None, "reference": None}

    current_price = float(row.get("listing_last_price") or 0.0)
    daily_vol_raw = row.get("signal_daily_volatility_pct")
    if current_price <= 0 or not daily_vol_raw:
        return {"mode": "market", "price": None, "reference": None}
    daily_vol = float(daily_vol_raw)

    ticker_name_map = row.get("ticker_name_map") or {}
    yahoo_symbol = ticker_name_map.get("YAHOO")

    if yahoo_symbol:
        try:
            vwap_result = _try_vwap_mode(yahoo_symbol, current_price, daily_vol, action)
            if vwap_result is not None:
                return vwap_result
        except Exception as err:
            logging.warning(f"⚠️ [ExecutionPriceAdvisor]: VWAP-режим недоступен для {row.get('symbol')} ({yahoo_symbol}): {err}")

    return _statistical_mode(db_instance, listing_id, current_price, daily_vol, action)


def _try_vwap_mode(yahoo_symbol: str, current_price: float, daily_vol: float, action: str):
    """None -- рынок не в REGULAR или сессия ещё не разогрелась (не ошибка, обычное состояние)."""
    info = yf.Ticker(yahoo_symbol).get_info()
    if info.get("marketState") != "REGULAR":
        return None

    hist = yf.Ticker(yahoo_symbol).history(period="1d", interval="1m")
    if hist.empty or len(hist) < settings.EXECUTION_TIMING_SESSION_WARMUP_MINUTES:
        return None  # сессия ещё не разогрелась -- статистический режим надёжнее

    hist = hist[hist["Volume"] > 0]
    if hist.empty:
        return None

    typical_price = (hist["High"] + hist["Low"] + hist["Close"]) / 3.0
    total_volume = float(hist["Volume"].sum())
    if total_volume <= 0:
        return None
    vwap = float((typical_price * hist["Volume"]).sum() / total_volume)
    if vwap <= 0:
        return None

    deviation_pct = (current_price - vwap) / vwap * 100.0
    threshold_pct = settings.EXECUTION_TIMING_VOLATILITY_MULTIPLIER * daily_vol

    if action == "BUY" and deviation_pct > threshold_pct:
        return {"mode": "limit", "price": round(vwap, 2), "reference": "vwap"}
    if action == "SELL" and deviation_pct < -threshold_pct:
        return {"mode": "limit", "price": round(vwap, 2), "reference": "vwap"}

    return {"mode": "market", "price": None, "reference": "vwap"}


def suggest_optimal_price(db_instance, listing_id: int, action: str) -> dict:
    """
    Шпаргалка «оптимальная цена» (Б) -- см. Claude/BACKLOG.md №122/123, вторая половина
    выбора наравне с suggest_execution_terms (шпаргалка «как можно скорее», А). В отличие
    от suggest_execution_terms -- не зависит от внутридневного движения/VWAP (этих данных
    может не быть, если рынок закрыт -- ровно случай, с которого началась тема) и не
    требует уже держащейся позиции (avg_price для входа ещё не существует) -- отсчитывается
    от ТЕКУЩЕЙ цены на момент решения, тем же переиспользуемым принципом K×волатильность
    ("гармошка"), что уже применяется у стоп-лосса/трейлинг-стопа/лесенки, просто с другим
    K (settings.OPTIMAL_PRICE_VOLATILITY_MULTIPLIER, откалиброван отдельным бэктестом,
    не то же K, что у стоп-лосса) и другим якорем (текущая цена, не avg_price/peak_price).

    Симметрично: для BUY -- скидка вниз (подожди, пока опустится), для SELL -- премия
    вверх (подожди чуть более высокой цены). Всегда даёт число, если есть цена и
    волатильность -- в отличие от suggest_execution_terms, здесь нет режима "market"
    без цены (сам смысл шпаргалки Б -- «вот конкретная цель, к которой стоит стремиться»).

    Возвращает {"price": float|None, "k": float} -- price=None, если по листингу нет
    честных данных (цена/волатильность).
    """
    action = str(action).upper().strip()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"action должен быть 'BUY' или 'SELL', получено: {action!r}")

    row = db_instance.execute_row("""
        SELECT listing_last_price, signal_daily_volatility_pct
        FROM public.v_listings_tickers
        WHERE listing_id = %s;
    """, (listing_id,))

    k = settings.OPTIMAL_PRICE_VOLATILITY_MULTIPLIER
    if not row:
        return {"price": None, "k": k}

    current_price = float(row.get("listing_last_price") or 0.0)
    daily_vol_raw = row.get("signal_daily_volatility_pct")
    if current_price <= 0 or not daily_vol_raw:
        return {"price": None, "k": k}
    daily_vol = float(daily_vol_raw)

    if action == "BUY":
        price = current_price * (1 - k * daily_vol / 100.0)
    else:
        price = current_price * (1 + k * daily_vol / 100.0)

    return {"price": round(price, 2), "k": k}


def _statistical_mode(db_instance, listing_id: int, current_price: float, daily_vol: float, action: str) -> dict:
    move = compute_price_move(db_instance, listing_id, current_price, settings.EXECUTION_TIMING_LOOKBACK_MINUTES)
    if move is None:
        return {"mode": "market", "price": None, "reference": None}
    pct_move, _actual_minutes = move

    expected_move_pct = (
        daily_vol
        * math.sqrt(settings.EXECUTION_TIMING_LOOKBACK_MINUTES / settings.TRADING_DAY_MINUTES)
        * settings.EXECUTION_TIMING_VOLATILITY_MULTIPLIER
    )

    if action == "BUY" and pct_move > expected_move_pct:
        limit_price = current_price * (1 - expected_move_pct / 100.0)
        return {"mode": "limit", "price": round(limit_price, 2), "reference": "recent"}
    if action == "SELL" and pct_move < -expected_move_pct:
        limit_price = current_price * (1 + expected_move_pct / 100.0)
        return {"mode": "limit", "price": round(limit_price, 2), "reference": "recent"}

    return {"mode": "market", "price": None, "reference": "recent"}
