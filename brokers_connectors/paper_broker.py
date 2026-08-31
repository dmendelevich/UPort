import logging

from analytics.ladder_step_watcher import LadderStepWatcher
from brokers_connectors.sync_strategy_asset_fb import SyncStrategyAssetFB

"""
Эмулятор брокера для бумажного портфеля (execution_mode='CONFIRM', см.
Claude/BACKLOG.md №117/119/122/123) -- единственное место в коде, которое знает
про execution_mode. Вызывается из ТОГО ЖЕ цикла котировок, что и LadderStepWatcher
(sync_quotes_fb.py, рядом с check_ladder_step_triggers). Условие "пора" -- то же
самое, что видит и человек в пинге (переиспользует LadderStepWatcher.evaluate_all()
напрямую, не дублирует и не расходится с ним) -- разница только в том, кто
исполняет: здесь эмулятор сам, вместо ожидания похода человека к брокеру.

Дальше -- ТОТ ЖЕ путь, что и реальные брокерские факты (см. brokers_connectors/
sync_account_fb.py): пишем новое количество/среднюю в assets, потом
SyncStrategyAssetFB.distribute_asset_delta -- без самостоятельной линковки к
Плану (Вопрос 5, BACKLOG №117): то же автосопоставление "голого" плана
(require_linked_order=False), что уже работает для рыночных покупок реальных
портфелей.

Упрощение этого захода (сознательно, обсуждено с пользователем 2026-08-17):
исполняет атомарно в момент, когда LadderStepWatcher впервые видит
condition_met -- НЕ эмулирует лимитный приказ, стоящий в стакане несколько
циклов ДО срабатывания с видимо зарезервированными деньгами (LadderStepWatcher
пересчитывает порог заново каждый раз, "не по сохранённому числу" -- у него нет
заранее выставленной неизменной цены, как у настоящего лимитного приказа на
бирже). Резерв/списание кэша (database.py::reserve_cash/release_reservation)
происходят одним движением в момент исполнения, не растянуты по времени.
Честная многодневная имитация "заявка уже стоит, деньги уже заблокированы" --
отдельное расширение, увязанное с тем, где рождается План (тема «К сделке»,
BACKLOG №122/123), не эта функция.
"""


def run_paper_broker_cycle(db_instance):
    """Вызывается из sync_quotes_fb.py тем же тиком, что и check_ladder_step_triggers."""
    watcher = LadderStepWatcher(db_instance)
    for item in watcher.evaluate_all():
        row, met = item["row"], item["met"]
        if not met:
            continue
        try:
            _try_fill(db_instance, row, met)
        except Exception as err:
            logging.error(f"⚠️ [PaperBroker]: Сбой исполнения pipeline_id={row.get('pipeline_id')}: {err}")


def _try_fill(db_instance, row: dict, met: dict):
    portfolio_id = int(row["portfolio_id"])

    # Живой баг, найден 2026-08-31: раньше проверялось `execution_mode == 'CONFIRM'` --
    # верно, ПОКА «ПБум» был единственным бумажным портфелем. С появлением «ПБумАвто»
    # (`AUTO`) и «ПБумКлод» (`ADVISORY`, Claude/BACKLOG.md, 2026-08-29) execution_mode
    # перестал быть надёжным признаком "это бумажный портфель" -- он отвечает на другой
    # вопрос (КАК исполняется решение), не на "реальные это деньги или нет". Оба новых
    # портфеля тихо никогда не исполнялись эмулятором, без единой ошибки в логе --
    # тот же класс бага, что уже чинили в database.py::get_test_capital_summary в тот же
    # день создания портфелей. Истинный признак -- portfolios.broker_id IS NULL.
    portfolio_row = db_instance.execute_row(
        "SELECT broker_id FROM public.portfolios WHERE id = %s;", (portfolio_id,)
    )
    if not portfolio_row or portfolio_row.get("broker_id") is not None:
        return  # эмулятор действует только на бумажных портфелях -- реальные не трогает

    qty = float(met["suggested_qty"])
    price = float(met["suggested_price"])
    if qty == 0 or price <= 0:
        return

    listing_id = int(row["listing_id"])
    ticker_id = int(row["ticker_id"])
    is_buy = qty > 0
    cost = abs(qty) * price

    if is_buy:
        if not db_instance.reserve_cash(portfolio_id, cost):
            logging.info(
                f"ℹ️ [PaperBroker]: Недостаточно кэша для {row['symbol']} "
                f"(портфель {portfolio_id}, нужно ${cost:,.2f}) -- пробую в следующем цикле."
            )
            return
        # Резерв и списание одним движением (см. докстринг файла -- упрощение этого
        # захода) -- лимитная заявка не может исполниться хуже своей цены, но здесь
        # met["suggested_price"] и есть цена исполнения, улучшения относительно
        # самого себя не бывает, actual_spent == reserved.
        db_instance.release_reservation(portfolio_id, reserved_amount=cost, actual_spent=cost)

    strat_sync = SyncStrategyAssetFB(db_instance)
    old_qty = strat_sync.get_current_quantity(portfolio_id, listing_id)
    new_qty = old_qty + qty  # qty уже несёт знак (лесенка продаж -- отрицательный target_quantity)

    if new_qty < 0:
        # Может случиться только на продаже (qty>0 к неотрицательному old_qty новый
        # остаток отрицательным быть не может) -- расчётная дельта продала бы больше,
        # чем реально держим (рассинхронизация данных). Кэш на этом пути ещё не
        # тронут (резерв выше -- только для покупки) -- просто отменяем цикл.
        logging.error(
            f"🚨 [PaperBroker]: Расчётная дельта продала бы больше, чем держим "
            f"(portfolio={portfolio_id}, listing={listing_id}, old={old_qty}, delta={qty}) -- пропускаю цикл."
        )
        return

    order_id = _write_synthetic_order(db_instance, portfolio_id, ticker_id, listing_id, qty, price)
    # Апсертим НОВОЕ количество (даже 0 -- строка ЕЩЁ не удаляется) ДО distribute_asset_delta:
    # _apply_strategy_balance ищет asset_id через SELECT по (portfolio_id, listing_id) -- если
    # удалить строку раньше, для полного выхода поиск проваливается и allocated_quantity/
    # order_pipelines не продвигаются (живой баг найден при тестировании -- у настоящего
    # брокерского синка та же ловушка: sync_account_fb.py вызывает distribute_asset_delta
    # ТОЛЬКО внутри цикла по отчитанным позициям broker'а, куда полностью проданная бумага
    # уже не попадает -- см. Claude/BACKLOG.md, разбор при отладке эмулятора).
    _upsert_asset_quantity(db_instance, portfolio_id, listing_id, old_qty, new_qty, price)

    if not is_buy:
        # Продажа -- пополняет кэш, резервирование неприменимо (см. ветку BUY выше).
        db_instance.execute_query(
            "UPDATE public.accounts SET cash_available = cash_available + %s, last_updated = CURRENT_TIMESTAMP "
            "WHERE portfolio_id = %s AND currency_id = 'USD';",
            (cost, portfolio_id)
        )

    strat_sync.distribute_asset_delta(
        portfolio_id=portfolio_id, listing_id=listing_id, ticker_id=ticker_id,
        old_qty=old_qty, new_qty=new_qty, price=price,
    )

    if new_qty == 0:
        db_instance.execute_query(
            "DELETE FROM public.assets WHERE portfolio_id = %s AND listing_id = %s;",
            (portfolio_id, listing_id)
        )

    db_instance.ensure_watchlist_row_v2(
        portfolio_id=portfolio_id, listing_id=listing_id,
        reason="bought" if is_buy else ("sold_out" if new_qty == 0 else "bought"),
    )

    db_instance.execute_query(
        """
            UPDATE public.accounts SET assets_value = (
                SELECT COALESCE(SUM(a.quantity * l.last_price), 0)
                FROM public.assets a JOIN public.listings l ON a.listing_id = l.id
                WHERE a.portfolio_id = %s
            ), last_updated = CURRENT_TIMESTAMP
            WHERE portfolio_id = %s AND currency_id = 'USD';
        """,
        (portfolio_id, portfolio_id)
    )

    logging.info(
        f"✅ [PaperBroker]: {'Куплено' if is_buy else 'Продано'} {abs(qty):.0f} шт {row['symbol']} "
        f"по ${price:,.2f} (портфель {portfolio_id}, синтетический ордер {order_id})."
    )


def _write_synthetic_order(db_instance, portfolio_id: int, ticker_id: int, listing_id: int, qty: float, price: float) -> str:
    """
    Синтетическая строка public.orders -- см. Claude/BACKLOG.md №117 (Вопрос 3):
    broker_order_id = "TEST-<portfolio_id>-<id>", где <id> -- собственный
    автоинкремент СУБД (гарантированно уникален и последователен, не пересекается
    с числовыми ID реального брокера). status='executed' сразу -- заявка и
    исполнение здесь одномоментны (см. докстринг файла).
    oper: 1 = покупка, 3 = продажа (та же конвенция, что daily_digest.py читает
    из реальных приказов ФБ). type=1 -- рыночная (эмулятор сегодня исполняет
    атомарно по текущей цене, отдельного лимитного "type=2, стоит и ждёт" пока нет).
    """
    result = db_instance.execute_query(
        """
            INSERT INTO public.orders (portfolio_id, ticker_id, listing_id, status, oper, type, q, p, currency_id)
            VALUES (%s, %s, %s, 'executed', %s, 1, %s, %s, 'USD')
            RETURNING id;
        """,
        (portfolio_id, ticker_id, listing_id, 1 if qty > 0 else 3, abs(qty), price)
    )
    order_pk = result[0]["id"] if isinstance(result, list) else result["id"]
    broker_order_id = f"TEST-{portfolio_id}-{order_pk}"
    db_instance.execute_query(
        "UPDATE public.orders SET broker_order_id = %s WHERE id = %s;",
        (broker_order_id, order_pk)
    )
    return broker_order_id


def _upsert_asset_quantity(db_instance, portfolio_id: int, listing_id: int, old_qty: float, new_qty: float, fill_price: float):
    """
    Пишет НОВОЕ абсолютное количество/среднюю в assets -- та же семантика, что и
    реальный брокерский синк (sync_account_fb.py): assets хранит факт, не дельту.
    Покупка -- средняя цена пересчитывается взвешенно (старое кол-во по старой
    средней + новое кол-во по цене исполнения); полностью новая позиция --
    средняя равна цене исполнения. Продажа -- средняя остаётся прежней (себестоимость
    ОСТАЮЩИХСЯ акций не меняется при частичном выходе).

    Полный выход (new_qty=0) -- строка НЕ удаляется здесь (это отдельный шаг ПОСЛЕ
    distribute_asset_delta, см. _try_fill) -- удалить раньше означало бы, что
    _apply_strategy_balance не найдёт asset_id и не продвинет order_pipelines/
    strategy_assets (живой баг найден при тестировании -- та же ловушка есть и у
    настоящего брокерского синка).
    """
    # Один SELECT на оба случая (раньше дублировался в каждой ветке) -- заодно даёт id
    # для UPDATE. SELECT-затем-UPDATE/INSERT, не ON CONFLICT (Claude/BACKLOG.md №128) --
    # гонки нет: единственный вызывающий (_try_fill, из run_paper_broker_cycle) идёт
    # последовательно в одном цикле котировок, эта строка assets больше никому не пишется
    # (реальный брокерский синк бумажных портфелей не касается, execution_mode='CONFIRM').
    existing_row = db_instance.execute_row(
        "SELECT id, avg_price FROM public.assets WHERE portfolio_id = %s AND listing_id = %s;",
        (portfolio_id, listing_id)
    )
    raw_avg_price = (existing_row or {}).get("avg_price")

    if new_qty > old_qty:
        old_avg_price = float(raw_avg_price or 0.0)
        bought_qty = new_qty - old_qty
        new_avg_price = (
            (old_qty * old_avg_price + bought_qty * fill_price) / new_qty
            if old_qty > 0 else fill_price
        )
    else:
        # Продажа (частичная) -- средняя цена держащегося остатка не меняется.
        new_avg_price = float(raw_avg_price or fill_price)

    if existing_row:
        db_instance.execute_query(
            "UPDATE public.assets SET quantity = %s, avg_price = %s, last_updated = CURRENT_TIMESTAMP WHERE id = %s;",
            (new_qty, new_avg_price, int(existing_row["id"]))
        )
        return
    db_instance.execute_query(
        """
            INSERT INTO public.assets (portfolio_id, listing_id, quantity, avg_price, currency_id, last_updated, position_opened_at)
            VALUES (%s, %s, %s, %s, 'USD', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """,
        (portfolio_id, listing_id, new_qty, new_avg_price)
    )
