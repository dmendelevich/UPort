import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_confirm_keyboard, generate_nav_back_keyboard
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.position_exit_evaluator import PositionExitEvaluator

router = Router()


def _system_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")


def _back_keyboard(portfolio_id: int, listing_id: int = 0):
    """
    Клавиатура после исполнения/отказа/ошибки -- по просьбе пользователя 2026-08-04
    (после "✅ Исполнено" было некуда вернуться, reply_markup=None). Если известен
    listing_id -- ведёт на карточку бумаги (тот же паттерн, что и у самой кнопки
    "Купить/Продать виртуально", см. BACKLOG.md №71); если нет (например, кандидат
    так и не легализован до листинга) -- только в главное меню.
    """
    if listing_id:
        return generate_nav_back_keyboard(
            one_step_back_text="🔙 К карточке бумаги",
            full_back_callback=MenuAction(action="view_ticker", portfolio_id=portfolio_id, listing_id=listing_id, sub_view="owner").pack()
        )
    return generate_nav_back_keyboard(menu_only=True)


def _refresh_assets_value_sql() -> str:
    """
    accounts.assets_value для реальных портфелей держит в актуальном состоянии
    брокерский синк (sync_account_fb.py) -- у бумажного портфеля такого синка нет,
    поэтому после каждой виртуальной сделки пересчитываем сами. Найдено 2026-08-03
    при тестировании экрана «Тестовый капитал» -- без этого он показывал $0.00 по
    акциям, хотя карточка портфеля (считает live через assets/listings) была верна.
    """
    return """
        UPDATE public.accounts SET assets_value = (
            SELECT COALESCE(SUM(a.quantity * l.last_price), 0)
            FROM public.assets a JOIN public.listings l ON a.listing_id = l.id
            WHERE a.portfolio_id = %s
        )
        WHERE portfolio_id = %s AND currency_id = 'USD';
    """


async def _resolve_listing(t_id: int):
    """
    Возвращает (True, listing_id, price) или (False, error_text, listing_id=0).
    Общая часть покупки (обычной и форс) -- легализует листинг у ФБ при
    необходимости, проверяет, что цена реально получена (см. BACKLOG.md №59).
    """
    listing_row = await asyncio.to_thread(
        db_sys.execute_row,
        "SELECT id, last_price FROM public.listings WHERE ticker_id = %s AND broker_id = 1;",
        (t_id,)
    )
    if not listing_row:
        try:
            listing_id = await asyncio.to_thread(db_sys.ensure_listing, t_id, 1)
        except Exception as e:
            return False, f"⚠️ Не удалось легализовать листинг: {e}", 0
        listing_row = await asyncio.to_thread(
            db_sys.execute_row,
            "SELECT id, last_price FROM public.listings WHERE id = %s;",
            (listing_id,)
        )

    listing_id = int(listing_row["id"])
    price = float(listing_row.get("last_price") or 0.0)
    if price <= 0:
        return False, "⚠️ Не удалось получить цену, попробуй позже.", listing_id
    return True, listing_id, price


async def _execute_virtual_buy(p_id: int, s_id: int, t_id: int, amount: float, is_manual_override: bool):
    """
    Общая запись виртуальной покупки -- используется и подтверждённой
    рекомендацией (paper_buy_yes), и ручным форс-оверрайдом (paper_buy_force_yes,
    BACKLOG.md №73). Пишет напрямую в assets/strategy_assets/order_pipelines/
    accounts -- НЕ через SyncStrategyAssetFB.distribute_asset_delta (та требует,
    чтобы assets уже существовала, для первой покупки её ещё нет).
    Возвращает (True, quantity, price, spent, listing_id) или (False, error_text, listing_id).
    """
    ok, listing_id_or_err, price_or_zero = await _resolve_listing(t_id)
    if not ok:
        return False, listing_id_or_err, price_or_zero
    listing_id, price = listing_id_or_err, price_or_zero

    quantity = max(1, round(amount / price))
    spent = quantity * price
    now = _system_now()

    sql = """
        WITH upsert_asset AS (
            INSERT INTO public.assets (portfolio_id, listing_id, quantity, avg_price, currency_id, last_updated, position_opened_at)
            VALUES (%s, %s, %s, %s, 'USD', %s, %s)
            ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET
                avg_price = (assets.quantity * assets.avg_price + EXCLUDED.quantity * EXCLUDED.avg_price) / (assets.quantity + EXCLUDED.quantity),
                quantity = assets.quantity + EXCLUDED.quantity,
                last_updated = EXCLUDED.last_updated
            RETURNING id
        ),
        upsert_strategy_asset AS (
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            SELECT id, %s, %s, %s FROM upsert_asset
            ON CONFLICT (asset_id, strategy_id) DO UPDATE SET
                allocated_quantity = strategy_assets.allocated_quantity + EXCLUDED.allocated_quantity,
                last_updated_at = EXCLUDED.last_updated_at
        ),
        new_pipeline AS (
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, strategy_id, ticker_id, current_step, pipeline_status, target_quantity, initial_entry_price, is_manual_override)
            VALUES (%s, %s, %s, %s, 1, 'COMPLETED', %s, %s, %s)
        ),
        updated_cash AS (
            UPDATE public.accounts SET cash_available = cash_available - %s, last_updated = %s
            WHERE portfolio_id = %s AND currency_id = 'USD'
        )
        SELECT 1;
    """
    params = (
        p_id, listing_id, quantity, price, now, now,
        s_id, quantity, now,
        p_id, listing_id, s_id, t_id, quantity, price, is_manual_override,
        spent, now, p_id,
    )
    await asyncio.to_thread(db_sys.execute_query, sql, params)
    await asyncio.to_thread(db_sys.execute_query, _refresh_assets_value_sql(), (p_id, p_id))
    # У бумажного портфеля нет sync_account_fb.py (реальный брокерский синк) -- он и
    # заводит watchlist для реальных портфелей при покупке. Без этого вызова позиции
    # «ПБум» физически не попадали бы во вкладку watchlist (найдено 2026-08-13).
    await asyncio.to_thread(db_sys.ensure_watchlist_row_v2, p_id, listing_id, "bought")
    return True, quantity, price, spent, listing_id


async def _execute_virtual_sell(p_id: int, l_id: int, s_id: int, ticker_id: int, asset_id: int, quantity: float, avg_price: float, is_manual_override: bool):
    """
    Общая запись виртуальной продажи -- используется и подтверждённой
    рекомендацией (paper_sell_yes), и ручным форс-оверрайдом (paper_sell_force_yes,
    BACKLOG.md №73). Полная продажа: DELETE из assets (каскадом уносит
    strategy_assets, FK ON DELETE CASCADE -- та же конвенция, что sync_account_fb.py
    для закрытых реальных позиций), запись order_pipelines с ОТРИЦАТЕЛЬНЫМ
    target_quantity и pipeline_status='COMPLETED' (конвенция _record_retroactive_exit
    из sync_strategy_asset_fb.py). Возвращает (True, price, proceeds, pnl, pnl_pct)
    или (False, error_text).
    """
    price_row = await asyncio.to_thread(
        db_sys.execute_row,
        "SELECT l.last_price FROM public.listings l WHERE l.id = %s;",
        (l_id,)
    )
    price = float((price_row or {}).get("last_price") or 0.0)
    if price <= 0:
        return False, "⚠️ Не удалось получить цену, попробуй позже."

    proceeds = quantity * price
    pnl = (price - avg_price) * quantity
    pnl_pct = ((price - avg_price) / avg_price * 100.0) if avg_price > 0 else 0.0
    now = _system_now()

    sql = """
        WITH deleted_asset AS (
            DELETE FROM public.assets WHERE id = %s
        ),
        new_pipeline AS (
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, strategy_id, ticker_id, current_step, pipeline_status, target_quantity, initial_entry_price, is_manual_override)
            VALUES (%s, %s, %s, %s, 1, 'COMPLETED', %s, 0, %s)
        ),
        updated_cash AS (
            UPDATE public.accounts SET cash_available = cash_available + %s, last_updated = %s
            WHERE portfolio_id = %s AND currency_id = 'USD'
        )
        SELECT 1;
    """
    params = (
        asset_id,
        p_id, l_id, s_id, ticker_id, -quantity, is_manual_override,
        proceeds, now, p_id,
    )
    await asyncio.to_thread(db_sys.execute_query, sql, params)
    await asyncio.to_thread(db_sys.execute_query, _refresh_assets_value_sql(), (p_id, p_id))
    # Тот же пробел, что и у покупки (см. _execute_virtual_buy) -- без реального
    # брокерского синка watchlist сам себя не обновит на полное закрытие позиции.
    await asyncio.to_thread(db_sys.ensure_watchlist_row_v2, p_id, l_id, "sold_out")
    return True, price, proceeds, pnl, pnl_pct


# send_paper_buy_recommendations/process_paper_buy_no -- убраны (Claude/BACKLOG.md
# №122/123, 2026-08-17), BUY бумажного портфеля переехал на «🤝 К сделке» прямо в
# дайджесте (bot_handlers/deal.py). process_paper_buy_yes ниже остаётся -- его же
# использует прямая кнопка «📥 Купить виртуально» на карточке тикера.

@router.callback_query(MenuAction.filter(F.action == "paper_buy_yes"))
async def process_paper_buy_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Подтверждение покупки -- перепроверяет условия заново (не исполняет старыми
    числами из утреннего сообщения). BACKLO.md №74 (живой кейс NRG, 2026-08-05):
    раньше требовалось точное совпадение с ЕДИНСТВЕННЫМ лидером рейтинга из
    evaluate_deployment -- клик на любого другого кандидата из топ-10 «Предложений»
    отваливался как "условия изменились", даже если тикер честно проходит экран
    стратегии. Теперь -- verify_buy_candidate (тикер не куплен, стратегия ещё не
    добрана, экран пройден сейчас), сумма -- compute_slot_size (та же формула слота,
    что и у форс-покупки), с тем же кэп по доступному кэшу. Для покупки вопреки
    совету -- см. paper_buy_force_yes (BACKLOG.md №73).
    """
    p_id = callback_data.portfolio_id
    s_id = callback_data.strategy_id
    t_id = callback_data.ticker_id

    await callback.answer("Проверяю условия...")

    advisor = CashDeploymentAdvisor(db_sys)
    match = await asyncio.to_thread(advisor.verify_buy_candidate, p_id, s_id, t_id)
    if not match:
        listing_row_chk = await asyncio.to_thread(
            db_sys.execute_row,
            "SELECT id FROM public.listings WHERE ticker_id = %s AND broker_id = 1;",
            (t_id,)
        )
        l_id_chk = int((listing_row_chk or {}).get("id") or 0)
        try:
            await callback.message.edit_text(
                "⚠️ Условия изменились, кандидат больше не актуален.",
                reply_markup=_back_keyboard(p_id, l_id_chk)
            )
        except TelegramBadRequest:
            pass
        return

    symbol = match["symbol"]
    strategy_name = match["strategy_name"]

    amount = await asyncio.to_thread(advisor.compute_slot_size, p_id, s_id)
    cash_row = await asyncio.to_thread(
        db_sys.execute_row, "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';", (p_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)
    amount = min(amount, cash_available)

    if amount <= 0:
        listing_row_chk = await asyncio.to_thread(
            db_sys.execute_row,
            "SELECT id FROM public.listings WHERE ticker_id = %s AND broker_id = 1;",
            (t_id,)
        )
        l_id_chk = int((listing_row_chk or {}).get("id") or 0)
        try:
            await callback.message.edit_text(
                f"⚠️ Не удалось исполнить {symbol} -- нет свободного кэша или размера слота.",
                reply_markup=_back_keyboard(p_id, l_id_chk)
            )
        except TelegramBadRequest:
            pass
        return

    result = await _execute_virtual_buy(p_id, s_id, t_id, amount, is_manual_override=False)
    if not result[0]:
        _, err_text, l_id_err = result
        try:
            await callback.message.edit_text(err_text, reply_markup=_back_keyboard(p_id, l_id_err))
        except TelegramBadRequest:
            pass
        return

    _, quantity, price, spent, listing_id = result
    logging.info(f"✅ [PaperExec]: Виртуально исполнено {quantity} шт {symbol} по ${price:,.2f} в '{strategy_name}' (портфель {p_id}).")
    try:
        await callback.message.edit_text(
            f"✅ Исполнено: {quantity} шт *{symbol}* по ${price:,.2f} (${spent:,.2f})",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(p_id, listing_id)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_buy_force_ask"))
async def process_paper_buy_force_ask(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Шаг подтверждения ручного форс-оверрайда покупки (BACKLOG.md №73, по просьбе
    пользователя 2026-08-04) -- система явно предупреждает, что НЕ рекомендует
    сделку, и показывает предпросмотр (сумма считается той же формулой слота,
    что и обычная рекомендация, см. CashDeploymentAdvisor.compute_slot_size, но
    без проверки "стратегия недофинансирована"), прежде чем позволить её сделать.
    """
    p_id = callback_data.portfolio_id
    s_id = callback_data.strategy_id
    t_id = callback_data.ticker_id

    await callback.answer()

    advisor = CashDeploymentAdvisor(db_sys)
    amount = await asyncio.to_thread(advisor.compute_slot_size, p_id, s_id)

    resolved = await _resolve_listing(t_id)
    if not resolved[0]:
        _, err_text, l_id_err = resolved
        try:
            await callback.message.edit_text(err_text, reply_markup=_back_keyboard(p_id, l_id_err))
        except TelegramBadRequest:
            pass
        return
    _, listing_id, price = resolved

    cash_row = await asyncio.to_thread(
        db_sys.execute_row, "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';", (p_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)
    amount = min(amount, cash_available)

    symbol_row = await asyncio.to_thread(db_sys.execute_row, "SELECT symbol FROM public.tickers WHERE id = %s;", (t_id,))
    symbol = (symbol_row or {}).get("symbol", "?")

    if amount <= 0:
        try:
            await callback.message.edit_text(
                f"⚠️ Не удалось посчитать сделку по {symbol} (нет свободного кэша или размера слота).",
                reply_markup=_back_keyboard(p_id, listing_id)
            )
        except TelegramBadRequest:
            pass
        return

    quantity = max(1, round(amount / price))
    keyboard = generate_confirm_keyboard(
        yes_text="✅ Да, всё равно",
        yes_callback_packed=MenuAction(action="paper_buy_force_yes", portfolio_id=p_id, strategy_id=s_id, ticker_id=t_id).pack(),
        no_text="❌ Нет",
        no_callback_packed=MenuAction(action="paper_buy_force_no", portfolio_id=p_id, listing_id=listing_id, strategy_id=s_id).pack(),
    )
    try:
        await callback.message.edit_text(
            f"⚠️ Система НЕ рекомендует эту покупку сейчас.\n\n"
            f"Планируется: ~{quantity} шт *{symbol}* на ${amount:,.2f} по рынку (~${price:,.2f}/шт).\n\n"
            f"Всё равно купить?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_buy_force_no"))
async def process_paper_buy_force_no(callback: types.CallbackQuery, callback_data: MenuAction):
    await callback.answer()
    try:
        await callback.message.edit_text("❌ Отменено.", reply_markup=_back_keyboard(callback_data.portfolio_id, callback_data.listing_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_buy_force_yes"))
async def process_paper_buy_force_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Исполнение форс-покупки -- в отличие от paper_buy_yes, НЕ проверяет
    CashDeploymentAdvisor (условия "недофинансирована" специально нет, в этом и
    смысл "Всё равно"). Пересчитывает сумму/цену заново (время прошло с экрана
    подтверждения) и пишет is_manual_override=TRUE, чтобы через год отличить от
    сделок по совету системы при измерении эффективности (BACKLOG.md №73).
    """
    p_id = callback_data.portfolio_id
    s_id = callback_data.strategy_id
    t_id = callback_data.ticker_id

    await callback.answer("Исполняю...")

    advisor = CashDeploymentAdvisor(db_sys)
    amount = await asyncio.to_thread(advisor.compute_slot_size, p_id, s_id)
    cash_row = await asyncio.to_thread(
        db_sys.execute_row, "SELECT cash_available FROM public.accounts WHERE portfolio_id = %s AND currency_id = 'USD';", (p_id,)
    )
    cash_available = float((cash_row or {}).get("cash_available") or 0.0)
    amount = min(amount, cash_available)

    symbol_row = await asyncio.to_thread(db_sys.execute_row, "SELECT symbol FROM public.tickers WHERE id = %s;", (t_id,))
    symbol = (symbol_row or {}).get("symbol", "?")

    if amount <= 0:
        try:
            await callback.message.edit_text(f"⚠️ Не удалось исполнить {symbol} -- нет свободного кэша.", reply_markup=_back_keyboard(p_id))
        except TelegramBadRequest:
            pass
        return

    result = await _execute_virtual_buy(p_id, s_id, t_id, amount, is_manual_override=True)
    if not result[0]:
        _, err_text, l_id_err = result
        try:
            await callback.message.edit_text(err_text, reply_markup=_back_keyboard(p_id, l_id_err))
        except TelegramBadRequest:
            pass
        return

    _, quantity, price, spent, listing_id = result
    logging.info(f"🔴 [PaperExec]: ФОРС-покупка {quantity} шт {symbol} по ${price:,.2f} (портфель {p_id}), вопреки совету системы.")
    try:
        await callback.message.edit_text(
            f"✅ Исполнено (вопреки совету): {quantity} шт *{symbol}* по ${price:,.2f} (${spent:,.2f})",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(p_id, listing_id)
        )
    except TelegramBadRequest:
        pass


# send_paper_sell_recommendations/process_paper_sell_no -- убраны (Claude/BACKLOG.md
# №122/123, 2026-08-17), SELL бумажного портфеля переехал на «🤝 К сделке» прямо в
# дайджесте (bot_handlers/deal.py). process_paper_sell_yes ниже остаётся -- его же
# использует прямая кнопка «📤 Продать виртуально» на карточке тикера.

@router.callback_query(MenuAction.filter(F.action == "paper_sell_yes"))
async def process_paper_sell_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Подтверждение продажи -- перепроверяет условия заново (цена могла отскочить,
    рекомендация могла смениться на HOLD). Для продажи вопреки совету -- см.
    paper_sell_force_yes (BACKLOG.md №73).
    """
    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    s_id = callback_data.strategy_id

    await callback.answer("Проверяю условия...")

    evaluator = PositionExitEvaluator(db_sys)
    alerts = await asyncio.to_thread(evaluator.evaluate_portfolio_exits, p_id)
    match = next(
        (a for a in alerts
         if a.get("recommendation") == "SELL" and int(a.get("listing_id", -1)) == l_id and int(a.get("strategy_id", -1)) == s_id),
        None
    )
    if not match:
        try:
            await callback.message.edit_text(
                "⚠️ Условия изменились, продажа больше не актуальна.",
                reply_markup=_back_keyboard(p_id, l_id)
            )
        except TelegramBadRequest:
            pass
        return

    symbol = match["symbol"]
    strategy_name = match["strategy_name"]
    quantity = float(match["quantity"])
    asset_id = int(match["asset_id"])
    ticker_id = int(match["ticker_id"])

    avg_price_row = await asyncio.to_thread(db_sys.execute_row, "SELECT avg_price FROM public.assets WHERE id = %s;", (asset_id,))
    avg_price = float((avg_price_row or {}).get("avg_price") or 0.0)

    result = await _execute_virtual_sell(p_id, l_id, s_id, ticker_id, asset_id, quantity, avg_price, is_manual_override=False)
    if not result[0]:
        try:
            await callback.message.edit_text(result[1], reply_markup=_back_keyboard(p_id, l_id))
        except TelegramBadRequest:
            pass
        return

    _, price, proceeds, pnl, pnl_pct = result
    logging.info(f"✅ [PaperExec]: Виртуально продано {quantity:g} шт {symbol} по ${price:,.2f} из '{strategy_name}' (портфель {p_id}), P&L ${pnl:,.2f}.")
    pnl_sign = "+" if pnl >= 0 else ""
    try:
        await callback.message.edit_text(
            f"✅ Продано: {quantity:g} шт *{symbol}* по ${price:,.2f} (${proceeds:,.2f})\n"
            f"P&L: {pnl_sign}${pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(p_id, l_id)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_sell_force_ask"))
async def process_paper_sell_force_ask(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Шаг подтверждения ручного форс-оверрайда продажи (BACKLOG.md №73, по просьбе
    пользователя 2026-08-04: "в реальной жизни я так и так смогу продать в
    терминале, даже если это глупо") -- система явно предупреждает, что НЕ
    рекомендует продажу, и показывает предпросмотр перед исполнением.
    """
    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    s_id = callback_data.strategy_id

    await callback.answer()

    asset_row = await asyncio.to_thread(
        db_sys.execute_row,
        """
            SELECT a.id AS asset_id, a.quantity, a.avg_price, l.last_price, l.ticker_id, t.symbol
            FROM public.assets a
            JOIN public.listings l ON a.listing_id = l.id
            JOIN public.tickers t ON l.ticker_id = t.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s;
        """,
        (p_id, l_id)
    )
    if not asset_row:
        try:
            await callback.message.edit_text("⚠️ Позиция уже не держится.", reply_markup=_back_keyboard(p_id, l_id))
        except TelegramBadRequest:
            pass
        return

    quantity = float(asset_row["quantity"])
    price = float(asset_row.get("last_price") or 0.0)
    symbol = asset_row["symbol"]
    proceeds = quantity * price

    keyboard = generate_confirm_keyboard(
        yes_text="✅ Да, всё равно",
        yes_callback_packed=MenuAction(action="paper_sell_force_yes", portfolio_id=p_id, listing_id=l_id, strategy_id=s_id).pack(),
        no_text="❌ Нет",
        no_callback_packed=MenuAction(action="paper_sell_force_no", portfolio_id=p_id, listing_id=l_id, strategy_id=s_id).pack(),
    )
    try:
        await callback.message.edit_text(
            f"⚠️ Система НЕ рекомендует продавать {symbol} сейчас.\n\n"
            f"Планируется: {quantity:g} шт по ~${price:,.2f} (${proceeds:,.2f}).\n\n"
            f"Всё равно продать?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_sell_force_no"))
async def process_paper_sell_force_no(callback: types.CallbackQuery, callback_data: MenuAction):
    await callback.answer()
    try:
        await callback.message.edit_text("❌ Отменено.", reply_markup=_back_keyboard(callback_data.portfolio_id, callback_data.listing_id))
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_sell_force_yes"))
async def process_paper_sell_force_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Исполнение форс-продажи -- в отличие от paper_sell_yes, НЕ проверяет
    PositionExitEvaluator (условия SELL специально нет, в этом и смысл "Всё
    равно"). Пишет is_manual_override=TRUE (BACKLOG.md №73).
    """
    p_id = callback_data.portfolio_id
    l_id = callback_data.listing_id
    s_id = callback_data.strategy_id

    await callback.answer("Исполняю...")

    asset_row = await asyncio.to_thread(
        db_sys.execute_row,
        """
            SELECT a.id AS asset_id, a.quantity, a.avg_price, l.ticker_id, t.symbol
            FROM public.assets a
            JOIN public.listings l ON a.listing_id = l.id
            JOIN public.tickers t ON l.ticker_id = t.id
            WHERE a.portfolio_id = %s AND a.listing_id = %s;
        """,
        (p_id, l_id)
    )
    if not asset_row:
        try:
            await callback.message.edit_text("⚠️ Позиция уже не держится.", reply_markup=_back_keyboard(p_id, l_id))
        except TelegramBadRequest:
            pass
        return

    asset_id = int(asset_row["asset_id"])
    quantity = float(asset_row["quantity"])
    avg_price = float(asset_row.get("avg_price") or 0.0)
    ticker_id = int(asset_row["ticker_id"])
    symbol = asset_row["symbol"]

    result = await _execute_virtual_sell(p_id, l_id, s_id, ticker_id, asset_id, quantity, avg_price, is_manual_override=True)
    if not result[0]:
        try:
            await callback.message.edit_text(result[1], reply_markup=_back_keyboard(p_id, l_id))
        except TelegramBadRequest:
            pass
        return

    _, price, proceeds, pnl, pnl_pct = result
    logging.info(f"🔴 [PaperExec]: ФОРС-продажа {quantity:g} шт {symbol} по ${price:,.2f} (портфель {p_id}), вопреки совету системы, P&L ${pnl:,.2f}.")
    pnl_sign = "+" if pnl >= 0 else ""
    try:
        await callback.message.edit_text(
            f"✅ Продано (вопреки совету): {quantity:g} шт *{symbol}* по ${price:,.2f} (${proceeds:,.2f})\n"
            f"P&L: {pnl_sign}${pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(p_id, l_id)
        )
    except TelegramBadRequest:
        pass

