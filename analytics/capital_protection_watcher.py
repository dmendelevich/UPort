import logging

import settings
from analytics.price_move_watcher import send_alert_notification

# public.alerts.condition_type -- VARCHAR(5), не влезают полные слова "stop_loss"/
# "trailing_stop" (найдено тестированием слоя 1, 2026-08-11) -- короткие коды для
# колонки, полное слово остаётся в Python-коде и в trigger_type (VARCHAR(100)).
_CONDITION_TYPE_CODE = {"stop_loss": "SL", "trailing_stop": "TS"}


def check_capital_protection(db_instance, broker_id: int = 1):
    """
    Защита капитала (см. Claude/19_price_move_protection_design.md, сигнал B) --
    универсальный слой поверх Револьверной/Трендовой/Консервативной (Индексное ядро
    не участвует -- диверсифицировано, обвал одной позиции невозможен, см. сигнал C).
    Не замена собственных правил выхода стратегий (тренд/фундаментал) -- отдельная,
    более быстрая защита ИМЕННО ДЕНЕГ поверх них.

    Два независимых, самостоятельных триггера на каждую позицию:
    - **Стоп-лосс** (`tactic_stop_loss_k`, все три стратегии) -- от `assets.avg_price`
      (цена входа), "сколько теряю от вложенного".
    - **Трейлинг-стоп** (`tactic_trailing_stop_k`, только Револьверная/Консервативная --
      Трендовой не нужен, откалибровано бэктестом день-за-днём на годовой истории,
      см. дизайн-документ) -- от `assets.peak_price_since_entry` (бегущий максимум с
      момента покупки, обновляется здесь же), "сколько отдаю от уже заработанного".

    Дистанция обоих = K × `signal_daily_volatility_pct`, БЕЗ временной составляющей
    (сознательно отличается от PriceMoveWatcher -- тот отвечает на другой вопрос:
    ожидаемое движение за конкретное окно, не "насколько ненормально движение
    независимо от срока держания"). Цена -- `listings.last_price` (брокерский слой,
    тот же, что и avg_price/peak -- НЕ tickers.current_price, тот обновляется раз в
    сутки и живёт в каноническом слое, смешивать нельзя, см. Claude/07_glossary.md).

    Консервативная во время активной лесенки (`active_entry_pipeline_id` не NULL) --
    трейлинг не проверяется вовсе: лесенка намеренно докупает на дальнейшей просадке,
    сигнал "продавай" в этот момент прямо противоречил бы собственной логике стратегии.

    Вызывается из цикла котировок (`sync_quotes_fb.py`), тот же ритм, что и
    `PriceMoveWatcher` -- рыночно-зависимая, "быстрая" проверка.
    """
    positions = db_instance.execute_query("""
        SELECT
            a.id AS asset_id, a.portfolio_id, a.listing_id, a.avg_price, a.peak_price_since_entry,
            lt.id AS ticker_id, lt.symbol, lt.listing_last_price AS current_price, lt.signal_daily_volatility_pct,
            s.id AS strategy_id, s.strategy_name, s.rules_config, tpl.system_key,
            op.id AS active_entry_pipeline_id,
            p.name AS portfolio_name, u.telegram_id
        FROM public.assets a
        JOIN public.v_listings_tickers lt ON a.listing_id = lt.listing_id
        JOIN public.strategy_assets sa ON sa.asset_id = a.id AND sa.allocated_quantity > 0
        JOIN public.strategies s ON sa.strategy_id = s.id
        JOIN public.strategy_templates tpl ON s.template_id = tpl.id
        JOIN public.portfolios p ON a.portfolio_id = p.id
        JOIN public.users u ON p.owner_id = u.id
        LEFT JOIN public.order_pipelines op
            ON op.portfolio_id = a.portfolio_id AND op.listing_id = a.listing_id
           AND op.strategy_id = s.id AND op.pipeline_status IN ('PENDING', 'ACTIVE')
           AND op.target_quantity > 0
        WHERE a.quantity > 0 AND lt.broker_id = %s
          AND tpl.system_key IN ('REVOLVER', 'TREND_FOLLOWING', 'CONSERVATIVE_ACCUMULATION')
          AND lt.listing_last_price IS NOT NULL AND lt.listing_last_price > 0;
    """, (broker_id,))
    positions = positions if isinstance(positions, list) else ([positions] if positions else [])

    active_pairs = set()
    for pos in positions:
        try:
            _check_one_position(db_instance, pos)
            active_pairs.add((int(pos["portfolio_id"]), int(pos["listing_id"])))
        except Exception as err:
            logging.error(f"⚠️ [CapitalProtection]: Сбой проверки asset_id={pos.get('asset_id')}: {err}")

    _resolve_orphaned_alerts(db_instance, active_pairs, broker_id)


def _check_one_position(db_instance, pos: dict):
    asset_id = int(pos["asset_id"])
    avg_price = float(pos["avg_price"] or 0.0)
    current_price = float(pos["current_price"] or 0.0)
    daily_vol_raw = pos.get("signal_daily_volatility_pct")
    system_key = pos["system_key"]
    rules = pos.get("rules_config") or {}

    if avg_price <= 0 or current_price <= 0 or not daily_vol_raw:
        return  # нет честных данных для нормировки -- не гадаем на плоском проценте
    daily_vol = float(daily_vol_raw)

    # Бегущий пик обновляется всегда, независимо от того, сработает ли что-то ниже --
    # он должен быть свежим к следующему циклу проверки, а не только в момент триггера.
    stored_peak = float(pos.get("peak_price_since_entry") or avg_price)
    new_peak = max(stored_peak, current_price)
    if new_peak != stored_peak:
        db_instance.execute_query(
            "UPDATE public.assets SET peak_price_since_entry = %s WHERE id = %s;",
            (new_peak, asset_id)
        )

    k_stop = rules.get("tactic_stop_loss_k")
    if k_stop:
        stop_price = avg_price * (1 - float(k_stop) * daily_vol / 100.0)
        if current_price <= stop_price:
            _handle_triggered(db_instance, pos, "stop_loss", current_price, avg_price)
            return  # один алерт за цикл на позицию -- стоп-лосс приоритетнее трейлинга

    k_trail = rules.get("tactic_trailing_stop_k")
    if k_trail:
        if system_key == "CONSERVATIVE_ACCUMULATION" and pos.get("active_entry_pipeline_id"):
            pass  # лесенка ещё докупается -- трейлинг намеренно не проверяем
        elif new_peak > avg_price * 1.005:  # реальный пик был, не шум на входе -- иначе это стоп-лосс
            trail_price = new_peak * (1 - float(k_trail) * daily_vol / 100.0)
            if current_price <= trail_price:
                _handle_triggered(db_instance, pos, "trailing_stop", current_price, new_peak)
                return

    _resolve_alerts(db_instance, pos["listing_id"], pos["portfolio_id"])


def _build_note(kind: str, portfolio_name: str, symbol: str, current_price: float, reference_price: float, strategy_name: str) -> str:
    pct = (current_price - reference_price) / reference_price * 100.0
    if kind == "stop_loss":
        label = "🔴 Стоп-лосс"
        ref_label = "входа"
    else:
        label = "🟡 Трейлинг-стоп"
        ref_label = "пика"
    text = f"{label}: {symbol} {pct:+.1f}% от цены {ref_label} (${reference_price:.2f} → ${current_price:.2f})."
    text += f" В портфеле {portfolio_name}, стратегия «{strategy_name}»."
    text += " Стоит рассмотреть продажу."
    return text


def _handle_triggered(db_instance, pos: dict, kind: str, current_price: float, reference_price: float):
    portfolio_id = int(pos["portfolio_id"])
    listing_id = int(pos["listing_id"])

    condition_code = _CONDITION_TYPE_CODE[kind]
    existing = db_instance.execute_row("""
        SELECT id,
               EXTRACT(EPOCH FROM ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - triggered_at))::int AS seconds_since
        FROM public.alerts
        WHERE portfolio_id = %s AND listing_id = %s
          AND source_type = 'uport' AND condition_type = %s AND is_active = true
        ORDER BY triggered_at DESC LIMIT 1;
    """, (portfolio_id, listing_id, condition_code))

    note = _build_note(kind, pos["portfolio_name"], pos["symbol"], current_price, reference_price, pos["strategy_name"])
    trigger_pct = round(abs((current_price - reference_price) / reference_price * 100.0), 2)

    if existing:
        seconds_since = int(existing.get("seconds_since") or 0)
        if seconds_since < settings.CAPITAL_PROTECTION_PERIODIC_SEC:
            return
        alert_id = int(existing["id"])
        db_instance.execute_query("""
            UPDATE public.alerts SET
                trigger_price = %s, trigger_pct = %s, note = %s,
                triggered_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
            WHERE id = %s;
        """, (current_price, trigger_pct, note, alert_id))
    else:
        insert_res = db_instance.execute_query("""
            INSERT INTO public.alerts (
                portfolio_id, listing_id, ticker_id, source_type, ticker, init_price, trigger_price,
                condition_type, trigger_type, expire_type, periodic, is_active, trigger_pct, note,
                triggered_at, created_at, updated_at
            ) VALUES (
                %s, %s, %s, 'uport', %s, %s, %s,
                %s, %s, '0', %s, true,
                %s, %s,
                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
            ) RETURNING id;
        """, (
            portfolio_id, listing_id, pos["ticker_id"], pos["symbol"], current_price, current_price,
            condition_code, f"{kind}_triggered", settings.CAPITAL_PROTECTION_PERIODIC_SEC,
            trigger_pct, note
        ))
        if not insert_res:
            return
        alert_id = int(insert_res[0]["id"])
        logging.info(f"🎯 [CapitalProtection]: Новый алерт #{alert_id} ({kind}) для {pos['symbol']} (портфель {portfolio_id})")

    send_alert_notification(pos.get("telegram_id"), note, alert_id, sell_action={
        "portfolio_id": portfolio_id, "strategy_id": pos["strategy_id"], "listing_id": listing_id,
    })


def _resolve_alerts(db_instance, listing_id: int, portfolio_id: int):
    """Условие больше не выполняется -- гасим взведённые алерты капиталозащиты этой позиции."""
    db_instance.execute_query("""
        UPDATE public.alerts SET is_active = false, updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
        WHERE listing_id = %s AND portfolio_id = %s AND source_type = 'uport'
          AND condition_type IN ('SL', 'TS') AND is_active = true;
    """, (listing_id, portfolio_id))


def _resolve_orphaned_alerts(db_instance, active_pairs: set, broker_id: int):
    """
    Алерт капиталозащиты живёт, пока позиция держится ВНУТРИ Р/Т/К (`check_capital_protection`
    выше отбирает позиции строго по этому условию каждый цикл). Если позиция целиком продана
    ИЛИ переведена вне Р/Т/К (например, брокерский синк увёл остаток в "Неопределённая" без
    совпавшего плана, см. sync_strategy_asset_fb.py) -- она перестаёт попадать в основную
    выборку, `_resolve_alerts` для неё больше никогда не вызывается, и алерт молча зависает
    активным навсегда с устаревшим текстом (найдено 2026-08-14, живой случай PG/П10 -- алерт
    продолжал называть "Револьверную", хотя карточка тикера уже честно показывала
    "Неопределённая"). Гасит именно потерю защиты, не мусор -- тот же эффект и для полностью
    проданной позиции, и для переноса в любую непокрываемую стратегию.
    """
    active_alerts = db_instance.execute_query("""
        SELECT al.id, al.portfolio_id, al.listing_id
        FROM public.alerts al
        JOIN public.listings l ON l.id = al.listing_id
        WHERE al.source_type = 'uport' AND al.condition_type IN ('SL', 'TS')
          AND al.is_active = true AND l.broker_id = %s;
    """, (broker_id,))
    active_alerts = active_alerts if isinstance(active_alerts, list) else ([active_alerts] if active_alerts else [])

    for al in active_alerts:
        key = (int(al["portfolio_id"]), int(al["listing_id"]))
        if key in active_pairs:
            continue
        db_instance.execute_query("""
            UPDATE public.alerts SET is_active = false, updated_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0)
            WHERE id = %s;
        """, (al["id"],))
        logging.info(f"🧹 [CapitalProtection]: Алерт #{al['id']} погашен -- позиция вышла из-под защиты (продана/переведена вне Р/Т/К).")
