import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest

from database import db_sys
from bot_handlers.common import MenuAction
from bot_handlers.bot_keyboards import generate_confirm_keyboard
from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.position_exit_evaluator import PositionExitEvaluator

router = Router()


def _system_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")


async def send_paper_buy_recommendations(db_instance, bot):
    """
    Фаза 2 темы «Бумажный портфель» (Claude/14_paper_portfolio.md) -- по каждому
    портфелю с execution_mode='CONFIRM' прогоняет CashDeploymentAdvisor и шлёт
    ОТДЕЛЬНОЕ сообщение с Да/Нет на каждую найденную рекомендацию. Область
    сознательно ограничена только покупкой (см. BACKLOG.md №60) -- выход/подрезка/
    лесенка добавятся отдельным шагом, когда портфель реально что-то купит.
    """
    portfolios = await asyncio.to_thread(
        db_instance.execute_query,
        "SELECT id, name, owner_id FROM public.portfolios WHERE execution_mode = 'CONFIRM';"
    )
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    for p in portfolios:
        p_id = int(p["id"])
        p_name = p["name"]
        owner_id = p.get("owner_id")

        owner_row = await asyncio.to_thread(
            db_instance.execute_row,
            f"SELECT telegram_id FROM public.users WHERE id = {int(owner_id)};"
        ) if owner_id else {}
        chat_id = (owner_row or {}).get("telegram_id")
        if not chat_id:
            logging.warning(f"⚠️ [PaperExec]: У владельца портфеля '{p_name}' (ID: {p_id}) нет telegram_id -- некому отправить подтверждение.")
            continue

        try:
            advisor = CashDeploymentAdvisor(db_instance)
            recommendations = await asyncio.to_thread(advisor.evaluate_deployment, p_id)
        except Exception as e:
            logging.error(f"❌ [PaperExec]: Не удалось посчитать рекомендации для '{p_name}' (ID: {p_id}): {e}")
            continue

        for rec in recommendations:
            if rec.get("status") != "CANDIDATE_FOUND":
                continue

            s_id = int(rec["strategy_id"])
            t_id = int(rec["ticker_id"])
            symbol = rec["symbol"]
            amount = float(rec["step1_amount_usd"])

            text = (
                f"💡 *{p_name}* — {rec['strategy_name']}\n"
                f"Кандидат: *{symbol}*\n"
                f"Слот: ${amount:,.2f} (шаг 1 лесенки, рынок)\n\n"
                f"{rec['reason']}\n\n"
                f"Исполнить виртуально?"
            )
            keyboard = generate_confirm_keyboard(
                yes_text="✅ Да",
                yes_callback_packed=MenuAction(action="paper_buy_yes", portfolio_id=p_id, strategy_id=s_id, ticker_id=t_id).pack(),
                no_text="❌ Нет",
                no_callback_packed=MenuAction(action="paper_buy_no", portfolio_id=p_id, strategy_id=s_id, ticker_id=t_id).pack(),
            )
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
                logging.info(f"✅ [PaperExec]: Рекомендация {symbol} для '{p_name}' отправлена на подтверждение.")
            except Exception as e:
                logging.error(f"❌ [PaperExec]: Не удалось отправить рекомендацию {symbol} для '{p_name}': {e}")


@router.callback_query(MenuAction.filter(F.action == "paper_buy_no"))
async def process_paper_buy_no(callback: types.CallbackQuery):
    """Отказ -- ничего не пишем в БД, отказ не запоминается: если условие всё ещё
    верно, кандидат просто появится снова на следующий день (как в обычном дайджесте)."""
    await callback.answer()
    try:
        await callback.message.edit_text("❌ Отклонено.", reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_buy_yes"))
async def process_paper_buy_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Подтверждение покупки -- перепроверяет условия заново (не исполняет старыми
    числами из утреннего сообщения), при необходимости легализует листинг у ФБ
    и пишет виртуальную сделку напрямую в assets/strategy_assets/order_pipelines/
    accounts -- НЕ через SyncStrategyAssetFB.distribute_asset_delta (та требует,
    чтобы assets уже существовала, для первой покупки её ещё нет).
    """
    p_id = callback_data.portfolio_id
    s_id = callback_data.strategy_id
    t_id = callback_data.ticker_id

    await callback.answer("Проверяю условия...")

    advisor = CashDeploymentAdvisor(db_sys)
    recommendations = await asyncio.to_thread(advisor.evaluate_deployment, p_id)
    match = next(
        (r for r in recommendations
         if r.get("status") == "CANDIDATE_FOUND" and int(r.get("strategy_id", -1)) == s_id and int(r.get("ticker_id", -1)) == t_id),
        None
    )
    if not match:
        try:
            await callback.message.edit_text("⚠️ Условия изменились, кандидат больше не актуален.", reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    symbol = match["symbol"]
    amount = float(match["step1_amount_usd"])
    strategy_name = match["strategy_name"]

    listing_row = await asyncio.to_thread(
        db_sys.execute_row,
        f"SELECT id, last_price FROM public.listings WHERE ticker_id = {t_id} AND broker_id = 1;"
    )
    if not listing_row:
        try:
            listing_id = await asyncio.to_thread(db_sys.ensure_listing, t_id, 1)
        except Exception as e:
            try:
                await callback.message.edit_text(f"⚠️ Не удалось легализовать листинг {symbol}: {e}", reply_markup=None)
            except TelegramBadRequest:
                pass
            return
        listing_row = await asyncio.to_thread(
            db_sys.execute_row,
            f"SELECT id, last_price FROM public.listings WHERE id = {int(listing_id)};"
        )

    listing_id = int(listing_row["id"])
    price = float(listing_row.get("last_price") or 0.0)

    if price <= 0:
        try:
            await callback.message.edit_text(f"⚠️ Не удалось получить цену {symbol}, попробуй позже.", reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    quantity = max(1, round(amount / price))
    spent = quantity * price
    now = _system_now()

    sql = f"""
        WITH upsert_asset AS (
            INSERT INTO public.assets (portfolio_id, listing_id, quantity, avg_price, currency_id, last_updated, position_opened_at)
            VALUES ({p_id}, {listing_id}, {quantity}, {price}, 'USD', '{now}', '{now}')
            ON CONFLICT (portfolio_id, listing_id) DO UPDATE SET
                avg_price = (assets.quantity * assets.avg_price + EXCLUDED.quantity * EXCLUDED.avg_price) / (assets.quantity + EXCLUDED.quantity),
                quantity = assets.quantity + EXCLUDED.quantity,
                last_updated = EXCLUDED.last_updated
            RETURNING id
        ),
        upsert_strategy_asset AS (
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            SELECT id, {s_id}, {quantity}, '{now}' FROM upsert_asset
            ON CONFLICT (asset_id, strategy_id) DO UPDATE SET
                allocated_quantity = strategy_assets.allocated_quantity + EXCLUDED.allocated_quantity,
                last_updated_at = EXCLUDED.last_updated_at
        ),
        new_pipeline AS (
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, strategy_id, ticker_id, current_step, pipeline_status, target_quantity, initial_entry_price)
            VALUES ({p_id}, {listing_id}, {s_id}, {t_id}, 1, 'COMPLETED', {quantity}, {price})
        ),
        updated_cash AS (
            UPDATE public.accounts SET cash_available = cash_available - {spent}, last_updated = '{now}'
            WHERE portfolio_id = {p_id} AND currency_id = 'USD'
        )
        SELECT 1;
    """
    await asyncio.to_thread(db_sys.execute_query, sql)

    logging.info(f"✅ [PaperExec]: Виртуально исполнено {quantity} шт {symbol} по ${price:,.2f} в '{strategy_name}' (портфель {p_id}).")
    try:
        await callback.message.edit_text(
            f"✅ Исполнено: {quantity} шт *{symbol}* по ${price:,.2f} (${spent:,.2f})",
            parse_mode="Markdown",
            reply_markup=None
        )
    except TelegramBadRequest:
        pass


async def send_paper_sell_recommendations(db_instance, bot):
    """
    Продолжение Фазы 2 -- второй тип подтверждения (BACKLOG.md №60/№65). Только
    ПОЛНЫЙ выход (recommendation='SELL') -- у Револьверной/Трендовой/Консервативной
    на фундаментальном сломе частичного выхода нет по замыслу (см. «Сделано» №33).
    'HOLD' с текстом "перенеси в Трендовую" сознательно пропускается -- это перенос
    между стратегиями, другое действие, есть свой готовый механизм.
    """
    portfolios = await asyncio.to_thread(
        db_instance.execute_query,
        "SELECT id, name, owner_id FROM public.portfolios WHERE execution_mode = 'CONFIRM';"
    )
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    for p in portfolios:
        p_id = int(p["id"])
        p_name = p["name"]
        owner_id = p.get("owner_id")

        owner_row = await asyncio.to_thread(
            db_instance.execute_row,
            f"SELECT telegram_id FROM public.users WHERE id = {int(owner_id)};"
        ) if owner_id else {}
        chat_id = (owner_row or {}).get("telegram_id")
        if not chat_id:
            logging.warning(f"⚠️ [PaperExec]: У владельца портфеля '{p_name}' (ID: {p_id}) нет telegram_id -- некому отправить подтверждение.")
            continue

        try:
            evaluator = PositionExitEvaluator(db_instance)
            alerts = await asyncio.to_thread(evaluator.evaluate_portfolio_exits, p_id)
        except Exception as e:
            logging.error(f"❌ [PaperExec]: Не удалось посчитать рекомендации на выход для '{p_name}' (ID: {p_id}): {e}")
            continue

        for alert in alerts:
            if alert.get("recommendation") != "SELL":
                continue

            l_id = int(alert["listing_id"])
            s_id = int(alert["strategy_id"])
            symbol = alert["symbol"]
            quantity = float(alert["quantity"])

            text = (
                f"📤 *{p_name}* — {alert['strategy_name']}\n"
                f"Позиция: *{symbol}* ({quantity:g} шт)\n\n"
                f"{alert['reason']}\n\n"
                f"Продать виртуально?"
            )
            keyboard = generate_confirm_keyboard(
                yes_text="✅ Да",
                yes_callback_packed=MenuAction(action="paper_sell_yes", portfolio_id=p_id, listing_id=l_id, strategy_id=s_id).pack(),
                no_text="❌ Нет",
                no_callback_packed=MenuAction(action="paper_sell_no", portfolio_id=p_id, listing_id=l_id, strategy_id=s_id).pack(),
            )
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
                logging.info(f"✅ [PaperExec]: Рекомендация продажи {symbol} для '{p_name}' отправлена на подтверждение.")
            except Exception as e:
                logging.error(f"❌ [PaperExec]: Не удалось отправить рекомендацию продажи {symbol} для '{p_name}': {e}")


@router.callback_query(MenuAction.filter(F.action == "paper_sell_no"))
async def process_paper_sell_no(callback: types.CallbackQuery):
    """Отказ -- ничего не пишем в БД, отказ не запоминается (как и у покупки)."""
    await callback.answer()
    try:
        await callback.message.edit_text("❌ Отклонено.", reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(MenuAction.filter(F.action == "paper_sell_yes"))
async def process_paper_sell_yes(callback: types.CallbackQuery, callback_data: MenuAction):
    """
    Подтверждение продажи -- перепроверяет условия заново (цена могла отскочить,
    рекомендация могла смениться на HOLD), затем полный выход: DELETE из assets
    (каскадом уносит strategy_assets, FK ON DELETE CASCADE -- та же конвенция,
    что уже использует sync_account_fb.py для закрытых реальных позиций), запись
    order_pipelines с ОТРИЦАТЕЛЬНЫМ target_quantity и pipeline_status='COMPLETED'
    (конвенция _record_retroactive_exit из sync_strategy_asset_fb.py), зачисление
    выручки в accounts.cash_available.
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
            await callback.message.edit_text("⚠️ Условия изменились, продажа больше не актуальна.", reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    symbol = match["symbol"]
    strategy_name = match["strategy_name"]
    quantity = float(match["quantity"])
    asset_id = int(match["asset_id"])
    ticker_id = int(match["ticker_id"])

    price_row = await asyncio.to_thread(
        db_sys.execute_row,
        f"SELECT a.avg_price, l.last_price FROM public.assets a JOIN public.listings l ON a.listing_id = l.id WHERE a.id = {asset_id};"
    )
    price = float((price_row or {}).get("last_price") or 0.0)
    avg_price = float((price_row or {}).get("avg_price") or 0.0)

    if price <= 0:
        try:
            await callback.message.edit_text(f"⚠️ Не удалось получить цену {symbol}, попробуй позже.", reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    proceeds = quantity * price
    pnl = (price - avg_price) * quantity
    pnl_pct = ((price - avg_price) / avg_price * 100.0) if avg_price > 0 else 0.0
    now = _system_now()

    sql = f"""
        WITH deleted_asset AS (
            DELETE FROM public.assets WHERE id = {asset_id}
        ),
        new_pipeline AS (
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, strategy_id, ticker_id, current_step, pipeline_status, target_quantity, initial_entry_price)
            VALUES ({p_id}, {l_id}, {s_id}, {ticker_id}, 1, 'COMPLETED', {-quantity}, 0)
        ),
        updated_cash AS (
            UPDATE public.accounts SET cash_available = cash_available + {proceeds}, last_updated = '{now}'
            WHERE portfolio_id = {p_id} AND currency_id = 'USD'
        )
        SELECT 1;
    """
    await asyncio.to_thread(db_sys.execute_query, sql)

    logging.info(f"✅ [PaperExec]: Виртуально продано {quantity:g} шт {symbol} по ${price:,.2f} из '{strategy_name}' (портфель {p_id}), P&L ${pnl:,.2f}.")
    pnl_sign = "+" if pnl >= 0 else ""
    try:
        await callback.message.edit_text(
            f"✅ Продано: {quantity:g} шт *{symbol}* по ${price:,.2f} (${proceeds:,.2f})\n"
            f"P&L: {pnl_sign}${pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)",
            parse_mode="Markdown",
            reply_markup=None
        )
    except TelegramBadRequest:
        pass
