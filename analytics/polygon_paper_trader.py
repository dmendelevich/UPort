import logging

import yfinance as yf

from analytics.analytics_utils import TickerEvaluator
from analytics import claude_paper_trader as cpt
from analytics.price_move_watcher import send_alert_notification

"""
«ПБумПолигон» (Claude/BACKLOG.md №170, 2026-09-05) -- живой (не бэктест)
испытательный стенд для находок тем #167/#168: экспериментальный жёсткий
SL/TP вместо реального трейлинга Револьверной, с VIX-предохранителем.

Архитектурно НЕ похож на auto_paper_trader.py (ПБумАвто, execution_mode='AUTO',
15-минутный цикл, Claude/BACKLOG.md №169) -- этот портфель execution_mode=
'ADVISORY' и НЕ должен попадать в run_auto_paper_cycle: тот применил бы
РЕАЛЬНЫЙ трейлинг/confirm_days Револьверной поверх позиций полигона, ровно то,
чего нужно избежать. Вместо этого -- собственный вызов run_polygon_cycle,
подвешенный на тот же 15-минутный рыночный цикл (sync_quotes_fb.py), но
работающий полностью независимо от стандартного конвейера CashDeploymentAdvisor/
PositionExitEvaluator/CapitalProtectionWatcher.

Вход -- РЕАЛЬНЫЙ экран Револьверной (TickerEvaluator.screen_universe_for_strategy,
та же _score_revolver, что и в продакшене) -- тема #168 не нашла причин его
трогать. Покупка/продажа технически идут через claude_paper_trader.py (тот же
путь, что и ПБумКлод) -- позиции лежат в "Неопределённая", куда не дотягивается
ни PositionExitEvaluator, ни CapitalProtectionWatcher (K не задан).

Выход -- СВОЙ простой SL/TP (не трейлинг, не confirm_days) -- единственный
автоматический режим: широкий набор (10%/10%), нашедший себя надёжным на ОБОИХ
чистых бычьих эпизодах темы #167. Узкий ("медвежий") набор сознательно НЕ
зашит сюда автоматически -- переключение между наборами проверено 8 способами
(тема #168) и ни разу не сработало лучше константы; если пользователь после
разбора остановки сочтёт нужным narrow-режим -- это его отдельное ручное
решение, не автоматика.

VIX-предохранитель -- останавливает НОВЫЕ покупки, если текущий VIX выходит за
диапазон, реально пройденный на всех 5 эпизодах серии #167/#168 (13.5-52.3).
Держимые позиции продолжают жить по своему SL/TP (не паникуем). Возобновление
-- ТОЛЬКО ручное (кнопка "▶️ Возобновить автопокупки" на карточке портфеля,
bot_handlers/portfolios.py) -- автоматический откат при возврате VIX в
диапазон сознательно не сделан, чтобы не породить тот же whipsaw, что убил
переключение режимов в теме #168.
"""

POLYGON_PORTFOLIO_ID = 21
OWNER_TELEGRAM_ID = 250720161  # dmend, owner_id=1

VIX_MIN, VIX_MAX = 13.5, 52.3
SL_PCT, TP_PCT = 10.0, 10.0
SLOT_USD = 1000.0  # тот же фиксированный слот, что и у реальной Револьверной
# (rules_config.tactic_slot_fixed_usd, см. cash_deployment_advisor.py::_compute_slot_cap)
# -- размер идеи не растёт вместе с капиталом, растёт число слотов. Живая находка
# 2026-09-06: было SLOT_USD=2000, скопировано со структуры "10% на слот" из бэктеста
# темы #167/#168, не связано ни с реальным слотом Револьверной, ни с её лимитом
# portfolio_max_asset_pct=5% ($1000/$20 000=5% -- совпадает естественно, $2000 давал
# бы 10%, вдвое больше лимита).
N_SLOTS = 20  # $20,000 / $1000 -- то же покрытие капитала (100%), что и раньше (10×$2000)


def _get_current_vix() -> float | None:
    try:
        hist = yf.Ticker("^VIX").history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logging.error(f"❌ [Polygon]: Не удалось получить VIX: {e}")
        return None


def _get_revolver_strategy_id(db_instance) -> int:
    row = db_instance.execute_row("""
        SELECT s.id FROM public.strategies s
        JOIN public.strategy_templates tpl ON s.template_id = tpl.id
        WHERE s.portfolio_id = %s AND tpl.system_key = 'REVOLVER';
    """, (POLYGON_PORTFOLIO_ID,))
    if not row:
        raise ValueError("У ПБумПолигон нет стратегии Револьверная -- запусти add_pbum_poligon.py")
    return int(row["id"])


def _check_vix_gate(db_instance) -> bool:
    """Возвращает True, если новые покупки разрешены сейчас."""
    portfolio = db_instance.execute_row(
        "SELECT auto_trading_paused FROM public.portfolios WHERE id = %s;", (POLYGON_PORTFOLIO_ID,)
    )
    already_paused = bool((portfolio or {}).get("auto_trading_paused"))

    vix = _get_current_vix()
    if vix is None:
        # Честно не знаем текущий VIX -- не трогаем состояние в обе стороны.
        return not already_paused

    in_range = VIX_MIN <= vix <= VIX_MAX

    if not in_range and not already_paused:
        reason = f"VIX={vix:.2f} вне проверенного диапазона [{VIX_MIN}, {VIX_MAX}]"
        db_instance.execute_query("""
            UPDATE public.portfolios
            SET auto_trading_paused = true,
                auto_trading_paused_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0),
                auto_trading_paused_reason = %s
            WHERE id = %s;
        """, (reason, POLYGON_PORTFOLIO_ID))
        logging.warning(f"🛑 [Polygon]: Автопокупки остановлены -- {reason}")
        send_alert_notification(
            OWNER_TELEGRAM_ID,
            f"🛑 ПБумПолигон: автопокупки остановлены.\n{reason}\n\n"
            f"Держимые позиции продолжают жить по своему SL/TP -- паники нет. "
            f"Нужен разбор ситуации и ручное решение о продолжении (кнопка «▶️ Возобновить автопокупки» на карточке портфеля)."
        )
        return False

    if in_range and already_paused:
        logging.info(f"ℹ️ [Polygon]: VIX={vix:.2f} снова в диапазоне -- пауза НЕ снимается автоматически, ждёт ручного решения.")
        return False

    return not already_paused


def _check_exits(db_instance):
    data = cpt.report(db_instance, POLYGON_PORTFOLIO_ID)
    for h in data["holdings"]:
        avg_price = float(h.get("avg_price") or 0)
        last_price = float(h.get("last_price") or 0)
        if avg_price <= 0 or last_price <= 0:
            continue
        change_pct = (last_price - avg_price) / avg_price * 100.0
        if change_pct <= -SL_PCT:
            result = cpt.sell(db_instance, int(h["listing_id"]), f"Полигон: стоп-лосс {change_pct:+.1f}%", POLYGON_PORTFOLIO_ID)
            if result.get("ok"):
                logging.info(f"🔴 [Polygon]: SL сработал -- {h['symbol']} {change_pct:+.1f}%.")
        elif change_pct >= TP_PCT:
            result = cpt.sell(db_instance, int(h["listing_id"]), f"Полигон: цель прибыли {change_pct:+.1f}%", POLYGON_PORTFOLIO_ID)
            if result.get("ok"):
                logging.info(f"🟢 [Polygon]: TP сработал -- {h['symbol']} {change_pct:+.1f}%.")


def _check_entries(db_instance, allow_buy: bool):
    if not allow_buy:
        return

    data = cpt.report(db_instance, POLYGON_PORTFOLIO_ID)
    cash = float(data["cash_available"])
    held_ticker_ids = {int(h["ticker_id"]) for h in data["holdings"]}
    # Слоты, занятые ещё не исполненными (рынок закрыт/недавно куплено) заявками --
    # без этого при частых 15-минутных прогонах до заливки paper_broker счёт слотов
    # обнулялся бы каждый цикл, плодя планы сверх N_SLOTS (живой баг, найден при
    # прямом тестировании 2026-09-05 -- 6 дублей ушли в лог как WARNING, седьмая
    # прошла как новая покупка, слотов стало бы 11).
    pending_symbols = {p["symbol"] for p in (data.get("pending_orders") or [])}
    open_slots = N_SLOTS - len(data["holdings"]) - len(pending_symbols)
    if open_slots <= 0 or cash < SLOT_USD:
        logging.info(f"🏝️ [Polygon]: Свободных слотов нет ({len(data['holdings'])} держим, {len(pending_symbols)} в заявках) -- нечего покупать в этом цикле.")
        return

    strategy_id = _get_revolver_strategy_id(db_instance)
    candidates = TickerEvaluator(db_instance).screen_universe_for_strategy(
        strategy_id, exclude_ticker_ids=held_ticker_ids, us_only=True
    )
    candidates = [c for c in candidates if c["symbol"] not in pending_symbols]

    filled = 0
    for cand in candidates:
        if filled >= open_slots or cash < SLOT_USD:
            break
        result = cpt.buy(
            db_instance, int(cand["ticker_id"]), SLOT_USD,
            thesis=f"Полигон: прошёл экран Револьверной (ranking={cand['ranking_value']:.2f}).",
            exit_criteria=f"Автоматически: SL=-{SL_PCT:.0f}% / TP=+{TP_PCT:.0f}% от цены входа.",
            portfolio_id=POLYGON_PORTFOLIO_ID,
        )
        if result.get("ok"):
            filled += 1
            cash -= SLOT_USD
            logging.info(f"🟢 [Polygon]: Куплено {result['symbol']} (план #{result['pipeline_id']}).")
        else:
            logging.warning(f"⚠️ [Polygon]: Покупка {cand.get('symbol')} не удалась: {result.get('error')}")


def run_polygon_cycle(db_instance):
    allow_buy = _check_vix_gate(db_instance)
    _check_exits(db_instance)
    _check_entries(db_instance, allow_buy)
