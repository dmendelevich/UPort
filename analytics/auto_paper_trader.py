import logging

from analytics.cash_deployment_advisor import CashDeploymentAdvisor
from analytics.position_exit_evaluator import PositionExitEvaluator
from analytics.portfolio_rebalancer import PortfolioRebalancer
from analytics.deal_planner import create_buy_plan, create_sell_plan, create_trim_plan, create_topup_plan
from analytics.price_move_watcher import send_alert_notification

"""
Движок «ПБумАвто» (execution_mode='AUTO', Claude/BACKLOG.md, 2026-08-29) --
исполняет ровно те же рекомендации, что человек видит в дайджесте и подтверждает
кнопкой «🤝 К сделке», но САМ, без ожидания клика: те же переиспользуемые функции
(analytics/deal_planner.py::create_*_plan), что уже создают order_pipelines (PENDING)
для реального/ПБум-подтверждаемого потока -- дальше их так же исполняет уже
существующий brokers_connectors/paper_broker.py (ничего не меняли в нём).

Сознательно НЕ покрывает (scope v1, по прецеденту исходной Фазы 2 «ПБум» -- не
проектировать вслепую): случай PositionExitEvaluator HOLD "перенеси в Трендовую,
цель достигнута, импульс жив" -- у алерта нет отдельного машиночитаемого поля
"это перенос", только текст reason на русском; парсить прозу для авто-действия --
не тот стандарт надёжности, что у остального проекта (см. прежние баги на
строковом сопоставлении strategy_name). Такие позиции просто продолжают жить
под своей стратегией до собственного SELL/тайм-аута -- не хуже, чем если бы
рекомендация осталась непрочитанной в реальном дайджесте.
"""


def run_auto_paper_cycle(db_instance):
    portfolios = db_instance.execute_query("""
        SELECT p.id, p.name, u.telegram_id
        FROM public.portfolios p
        JOIN public.users u ON u.id = p.owner_id
        WHERE p.execution_mode = 'AUTO';
    """)
    portfolios = portfolios if isinstance(portfolios, list) else ([portfolios] if portfolios else [])

    processed = 0
    errors = 0
    for p in portfolios:
        p_id = int(p["id"])
        p_name = p["name"]
        telegram_id = p.get("telegram_id")
        try:
            actions = _run_one_portfolio(db_instance, p_id)
            processed += 1
            if actions:
                lines = "\n".join(actions)
                send_alert_notification(telegram_id, f"🤖 «{p_name}» -- автоматически исполнено:\n{lines}")
                logging.info(f"🤖 [AutoPaperTrader]: '{p_name}' (ID: {p_id}) -- {len(actions)} действие(й).")
            else:
                logging.info(f"🤖 [AutoPaperTrader]: '{p_name}' (ID: {p_id}) -- нечего исполнять.")
        except Exception as e:
            errors += 1
            logging.error(f"❌ [AutoPaperTrader]: Сбой цикла для '{p_name}' (ID: {p_id}): {e}")

    return {"processed": len(portfolios), "errors": errors}


def _run_one_portfolio(db_instance, p_id: int) -> list:
    actions = []

    # 1) Покупка -- CashDeploymentAdvisor, один или несколько недофинансированных
    # стратегий сразу (та же структура ответа, что видит "К сделке"/дайджест).
    for candidate in CashDeploymentAdvisor(db_instance).evaluate_deployment(p_id):
        if candidate.get("status") != "CANDIDATE_FOUND":
            continue
        result = create_buy_plan(db_instance, p_id, int(candidate["strategy_id"]), int(candidate["ticker_id"]), "asap")
        if result.get("ok"):
            actions.append(f"🟢 Покупка: {result['qty']} шт {result['symbol']} (план #{result['pipeline_id']})")
        else:
            logging.warning(f"⚠️ [AutoPaperTrader]: Покупка не удалась (портфель {p_id}): {result.get('error')}")

    # 2) Полный выход -- PositionExitEvaluator, только явный SELL (см. докстринг
    # модуля -- перенос между стратегиями сознательно не автоматизирован в v1).
    for alert in PositionExitEvaluator(db_instance).evaluate_portfolio_exits(p_id):
        if alert.get("recommendation") != "SELL":
            continue
        result = create_sell_plan(db_instance, p_id, int(alert["strategy_id"]), int(alert["listing_id"]), "asap")
        if result.get("ok"):
            actions.append(f"🔴 Продажа: {result['qty']:g} шт {result['symbol']} (план #{result['pipeline_id']})")
        else:
            logging.warning(f"⚠️ [AutoPaperTrader]: Продажа не удалась (портфель {p_id}): {result.get('error')}")

    # 3) Подрезка/докупка -- PortfolioRebalancer (сегодня только Консервативная).
    for alert in PortfolioRebalancer(db_instance).evaluate_portfolio(p_id):
        rec = alert.get("recommendation")
        if rec == "TRIM_DOWN":
            result = create_trim_plan(db_instance, p_id, int(alert["strategy_id"]), int(alert["listing_id"]))
            if result.get("ok"):
                actions.append(f"✂️ Подрезка: {result['qty']} шт {result['symbol']} (план #{result['pipeline_id']})")
            else:
                logging.warning(f"⚠️ [AutoPaperTrader]: Подрезка не удалась (портфель {p_id}): {result.get('error')}")
        elif rec == "TOP_UP":
            result = create_topup_plan(db_instance, p_id, int(alert["strategy_id"]), int(alert["listing_id"]), "asap")
            if result.get("ok"):
                actions.append(f"🔼 Докупка: {result['qty']} шт {result['symbol']} (план #{result['pipeline_id']})")
            else:
                logging.warning(f"⚠️ [AutoPaperTrader]: Докупка не удалась (портфель {p_id}): {result.get('error')}")

    return actions
