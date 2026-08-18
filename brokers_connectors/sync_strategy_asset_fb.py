import os
import json
import logging
import requests
from datetime import datetime, timezone

from analytics.analytics_utils import expected_step_quantity

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

class SyncStrategyAssetFB:
    def __init__(self, db_sys):
        """
        Менеджер распределения активов по виртуальным стратегиям.
        Принимает готовый инстанс db_sys для работы с PostgreSQL.
        """
        self.db = db_sys

    def get_current_quantity(self, portfolio_id: int, listing_id: int) -> float:
        """
        Метод 1: Замеряет текущее фактическое количество акций в общем котле assets.
        Вызывается ДО того, как демон применит обновление от брокера.
        """
        # Страховочный запрос. Ищем позицию по связке портфеля и листинга
        sql = """
            SELECT quantity
            FROM public.assets
            WHERE portfolio_id = %s
              AND listing_id = %s;
        """
        # Используем безопасный метод одной строки без скобок
        row = self.db.execute_row(sql, (portfolio_id, listing_id))
        if 'quantity' in row:
            return float(row['quantity'])

        # Если записи в assets нет (абсолютно новая бумага) — возвращаем 0.0
        return 0.0

    def distribute_asset_delta(self, portfolio_id: int, listing_id: int, ticker_id: int, old_qty: float, new_qty: float, price: float = None):
        """
        Метод 2: Главный диспетчер распределения. Вычисляется дельта движения акций
        (плюс при покупке, минус при продаже) и зачисляется в нужную стратегию.

        price -- цена исполнения, если вызывающий её знает (см. _notify_step_filled) --
        прокидывается только в уведомление, на саму дельту/бухгалтерию не влияет.
        """
        delta = new_qty - old_qty

        # Если баланс у брокера не изменился (например, просто обновилась цена), ничего не делаем
        if delta == 0.0:
            return

        # Шаг 1: Ищем план, явно ожидающий эту дельту (привязанный ордер -- см. bot_handlers/order_pipelines.py),
        # и считаем частичный зачёт вместо "всё или ничего" (см. Claude/09_pipeline_reconciliation.md)
        target_strategy_id, pipeline_id, action, matched_qty, excess_qty = self._find_target_strategy(portfolio_id, ticker_id, delta)

        # Шаг 2: Если план не найден (нет привязанного ордера, либо ждут сразу несколько -- см. ниже),
        # вся дельта летит в "Нераспределенные" (Шаблон №5), как раньше
        if target_strategy_id is None:
            unalloc_id = self._get_unallocated_strategy_id(portfolio_id)
            self._apply_strategy_balance(portfolio_id, listing_id, unalloc_id, delta)
            logging.info(f"ℹ️ [UPort Стратегии]: Дельта {delta} по ticker_id {ticker_id} ушла в 'Нераспределенные' (нет однозначно ожидающего плана)")
            return

        # Шаг 3: Основная (ожидаемая планом) часть -- в стратегию; излишек сверх плана -- в Нераспределенные
        self._apply_strategy_balance(portfolio_id, listing_id, target_strategy_id, matched_qty)
        if excess_qty != 0:
            unalloc_id = self._get_unallocated_strategy_id(portfolio_id)
            self._apply_strategy_balance(portfolio_id, listing_id, unalloc_id, excess_qty)
            logging.info(f"ℹ️ [UPort Стратегии]: Излишек {excess_qty} по ticker_id {ticker_id} (сверх плана {matched_qty}) ушёл в 'Нераспределенные'")

        if action == 'DIRECT_SELL_NO_PLAN':
            # Экстренная продажа без предварительного плана -- бумага держалась ровно в
            # одной стратегии, гадать было не нужно (см. Claude/11_asset_lifecycle_and_plan.md).
            # Задним числом пишем COMPLETED-запись, чтобы история циклов не терялась даже
            # для продаж "в обход" бота.
            logging.info(f"✅ [UPort Стратегии]: Экстренная продажа (стратегия ID: {target_strategy_id}) распределена без предварительного плана -- пишу историю задним числом.")
            new_pipe_id = self._record_retroactive_exit(portfolio_id, listing_id, ticker_id, target_strategy_id, matched_qty)
            if new_pipe_id:
                self._notify_step_filled(new_pipe_id, 'COMPLETE_PIPELINE', matched_qty, price=price)
        elif action in ('NEXT_STEP', 'COMPLETE_PIPELINE'):
            logging.info(f"✅ [UPort Стратегии]: Шаг плана (стратегия ID: {target_strategy_id}) засчитан -- {action}")
            # Уведомление -- ДО обновления статуса конвейера, пока current_step/pending_broker_order_id
            # ещё не сброшены (см. Claude/09_pipeline_reconciliation.md, реального "срочного канала" не
            # требуется -- вызов сидит на уже существующем реальном времени WebSocket-триггера ФБ)
            self._notify_step_filled(pipeline_id, action, matched_qty, price=price)
            self._update_pipeline_status(pipeline_id, action)
        elif action == 'PARTIAL_NO_ADVANCE':
            logging.info(f"ℹ️ [UPort Стратегии]: Частичное исполнение шага (стратегия ID: {target_strategy_id}) -- зачтено в план, шаг пока не продвинут (ждём остаток заявки)")

    def _find_waiting_pipeline(self, portfolio_id: int, ticker_id: int, require_linked_order: bool):
        """
        Ищет ровно один план (PENDING/ACTIVE), ожидающий эту бумагу -- либо строго с уже
        привязанным ордером (require_linked_order=True, ручная привязка через бота), либо
        "голый" план без ордера вообще (require_linked_order=False -- "План входа", создан
        проактивно до всякой покупки, см. Claude/11_asset_lifecycle_and_plan.md). Больше одного
        кандидата -- не гадаем, чей это ордер/покупка (см. Claude/09_pipeline_reconciliation.md).
        """
        # condition -- фиксированный структурный фрагмент (не из пользовательского ввода),
        # placeholder не годится, остаётся f-строкой (см. Claude/BACKLOG.md №81).
        condition = "pending_broker_order_id IS NOT NULL" if require_linked_order else "pending_broker_order_id IS NULL"
        sql = f"""
            SELECT id, strategy_id, current_step, target_quantity
            FROM public.order_pipelines
            WHERE portfolio_id = %s
              AND ticker_id = %s
              AND pipeline_status IN ('PENDING', 'ACTIVE')
              AND {condition};
        """
        pipes = self.db.execute_query(sql, (portfolio_id, ticker_id))
        pipes = pipes if isinstance(pipes, list) else ([pipes] if pipes else [])

        if len(pipes) == 0:
            return None
        if len(pipes) > 1:
            kind = "с привязанным ордером" if require_linked_order else "без привязанного ордера ('План входа')"
            logging.warning(f"⚠️ [UPort Стратегии]: {len(pipes)} планов {kind} одновременно ждут ticker_id {ticker_id} в портфеле {portfolio_id} -- дельта не может быть однозначно сопоставлена.")
            return None
        return pipes[0]

    def _find_single_holder_strategy(self, portfolio_id: int, ticker_id: int):
        """
        Третий, последний уровень поиска -- только для ПРОДАЖ (см. _find_target_strategy).
        Если бумага сейчас реально держится ровно в ОДНОЙ стратегии, продажа однозначно
        относится к ней -- ответ уже есть в strategy_assets, план заводить не нужно (см.
        Claude/11_asset_lifecycle_and_plan.md, обсуждение "срочной продажи без плана").
        Держится в нескольких стратегиях сразу -- настоящая неоднозначность, не резолвим.
        Возвращает (strategy_id, allocated_quantity) или None.
        """
        sql = """
            SELECT sa.strategy_id, sa.allocated_quantity
            FROM public.strategy_assets sa
            JOIN public.assets a ON sa.asset_id = a.id
            JOIN public.listings l ON a.listing_id = l.id
            WHERE a.portfolio_id = %s AND l.ticker_id = %s
              AND sa.allocated_quantity > 0;
        """
        rows = self.db.execute_query(sql, (portfolio_id, ticker_id))
        rows = rows if isinstance(rows, list) else ([rows] if rows else [])

        if len(rows) != 1:
            return None
        return int(rows[0]["strategy_id"]), float(rows[0]["allocated_quantity"])

    def _record_retroactive_exit(self, portfolio_id: int, listing_id: int, ticker_id: int, strategy_id: int, matched_qty: float):
        """
        Пишет задним числом COMPLETED-план для продажи без предварительного "Плана выхода"
        (см. Claude/11_asset_lifecycle_and_plan.md) -- чтобы история циклов не терялась даже
        для срочных продаж, сделанных в обход бота. Цена -- assets.avg_price на момент записи
        (нет привязанного ордера, откуда брать цену исполнения, см. _notify_step_filled).
        """
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
        result = self.db.execute_query("""
            INSERT INTO public.order_pipelines
                (portfolio_id, listing_id, ticker_id, strategy_id, current_step, pipeline_status,
                 target_quantity, initial_entry_price, pending_broker_order_id, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 1, 'COMPLETED',
                 %s, 0, NULL, %s, %s)
            RETURNING id;
        """, (portfolio_id, listing_id, ticker_id, strategy_id, matched_qty, system_now, system_now))
        if not result:
            logging.error(f"🚨 [UPort Стратегии]: Не удалось записать задним числом план продажи (портфель {portfolio_id}, тикер {ticker_id}, стратегия {strategy_id}).")
            return None
        pipe_id = result[0]["id"] if isinstance(result, list) else result["id"]
        logging.info(f"📝 [UPort Стратегии]: Задним числом записан план продажи #{pipe_id} (портфель {portfolio_id}, тикер {ticker_id}, стратегия {strategy_id}, {matched_qty} шт).")
        return pipe_id

    def _find_target_strategy(self, portfolio_id: int, ticker_id: int, delta: float):
        """
        Внутренний аналитический узел. Ищет план, ожидающий эту дельту -- сначала строго
        с привязанным ордером (pending_broker_order_id, привязка вручную через бота,
        bot_handlers/order_pipelines.py), а если такого нет -- "голый" план без ордера
        ("План входа", покупка рынком тоже должна поймать заранее созданный план, не только
        лимитный ордер, привязанный вручную -- согласовано 2026-07-29). Оба случая снимают
        неоднозначность "бумага в двух стратегиях" одинаково -- через "ровно один кандидат".
        Возвращает (strategy_id, pipeline_id, action, matched_qty, excess_qty).
        """
        # Жестко отсекаем дробную часть у прилетевшей от брокера дельты
        clean_delta = int(delta)

        # Если движение составило меньше 1 акции (например, дробный хвост), игнорируем
        if clean_delta == 0:
            return None, None, None, 0, 0

        pipe = self._find_waiting_pipeline(portfolio_id, ticker_id, require_linked_order=True)
        if pipe is None:
            pipe = self._find_waiting_pipeline(portfolio_id, ticker_id, require_linked_order=False)

        # Ни одного плана вообще -- ни привязанного, ни "голого". Для ПРОДАЖИ (delta<0)
        # это не обязательно тупик: если бумага сейчас держится ровно в одной стратегии,
        # гадать не нужно -- ответ уже есть в strategy_assets (см. _find_single_holder_strategy,
        # Claude/11_asset_lifecycle_and_plan.md, "срочная продажа без плана"). Для покупки
        # неоднозначность настоящая (новых денег ещё нигде нет) -- туда этот путь не идёт.
        if pipe is None:
            if clean_delta < 0:
                holder = self._find_single_holder_strategy(portfolio_id, ticker_id)
                if holder is not None:
                    strat_id, held_qty = holder
                    if abs(clean_delta) <= held_qty:
                        return strat_id, None, 'DIRECT_SELL_NO_PLAN', clean_delta, 0
            return None, None, None, 0, 0

        pipe_id = int(pipe['id'])
        strat_id = int(pipe['strategy_id'])
        curr_step = int(pipe['current_step'])
        target_qty = int(float(pipe['target_quantity']))  # Знак сохраняется: >0 вход, <0 выход (лесенка продаж)

        if target_qty == 0:
            return None, None, None, 0, 0

        # Дельта должна двигаться в ту же сторону, что и план (покупка ожидает покупку, продажа -- продажу)
        if (clean_delta > 0) != (target_qty > 0):
            return None, None, None, 0, 0

        # Проверка 1: дельта закрывает весь план целиком (или больше)
        if abs(clean_delta) >= abs(target_qty):
            excess_qty = clean_delta - target_qty
            return strat_id, pipe_id, 'COMPLETE_PIPELINE', target_qty, excess_qty

        # Проверка 2: дельта относительно текущего шага лесенки (через strategy_tactics)
        sql_tactic = """
            SELECT budget_share_pct
            FROM public.strategy_tactics
            WHERE strategy_id = %s
              AND step_number = %s;
        """
        tactic = self.db.execute_row(sql_tactic, (strat_id, curr_step))
        if tactic:
            expected_step_qty = expected_step_quantity(target_qty, tactic['budget_share_pct'])

            if abs(clean_delta) >= abs(expected_step_qty):
                excess_qty = clean_delta - expected_step_qty
                return strat_id, pipe_id, 'NEXT_STEP', expected_step_qty, excess_qty

        # Дельта в нужную сторону, но меньше ожидаемого шага (частичное исполнение лимитного
        # ордера, ещё не всё) -- засчитываем целиком в план, шаг пока не продвигаем. Tier 0 не
        # различает "довыполнится позже" от "и так и было" -- см. Claude/09_pipeline_reconciliation.md,
        # задача №6 (история сделок брокера) даст точность позже.
        return strat_id, pipe_id, 'PARTIAL_NO_ADVANCE', clean_delta, 0

    def _get_unallocated_strategy_id(self, portfolio_id: int) -> int:
        """
        Находит ID буферной стратегии для данного портфеля через связь с
        "заводским" шаблоном (system_key = 'UNALLOCATED'), а не по тексту имени.
        """
        sql = """
            SELECT s.id
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = %s
              AND st.system_key = 'UNALLOCATED';
        """
        # Безопасно забираем одну строку буфера
        row = self.db.execute_row(sql, (portfolio_id,))
        if row:
            return int(row['id'])

        # Жесткий аварийный fallback, если стратегии-буфера вдруг нет в базе
        raise ValueError(f"Критическая ошибка: В портфеле {portfolio_id} отсутствует буферная стратегия (system_key='UNALLOCATED')!")

    def _apply_strategy_balance(self, portfolio_id: int, listing_id: int, strategy_id: int, delta: float):
        """
        Внутренний бухгалтерский узел. Зачисляет или списывает дельту (включая отрицательные числа)
        в таблицу strategy_assets. Использует UPSERT (ON CONFLICT).
        """
        # Стерильный UTC-срез времени UPort без микросекунд и таймзон
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
        
        # Получаем ID связи из assets, так как таблица strategy_assets жестко привязана к общему котлу
        sql_asset = "SELECT id FROM public.assets WHERE portfolio_id = %s AND listing_id = %s;"
        asset_row = self.db.execute_row(sql_asset, (portfolio_id, listing_id))

        if not asset_row:
            logging.error(f"🚨 [UPort]: Не удалось найти asset_id для листинга {listing_id} портфеля {portfolio_id}")
            return

        asset_id = int(asset_row['id'])

        # Магия сквозной математики: просто прибавляем дельту (неважно, +5 или -5)
        sql_upsert = """
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (asset_id, strategy_id)
            DO UPDATE SET
                allocated_quantity = public.strategy_assets.allocated_quantity + EXCLUDED.allocated_quantity,
                last_updated_at = EXCLUDED.last_updated_at;
        """
        self.db.execute_query(sql_upsert, (asset_id, strategy_id, delta, system_now))

    def _notify_step_filled(self, pipeline_id: int, action: str, matched_qty: float, price: float = None):
        """
        Присылает владельцу портфеля сообщение в Telegram о том, что шаг плана исполнен.
        Вызывается ДО _update_pipeline_status, пока current_step и pending_broker_order_id
        ещё не сброшены. Сбой отправки не должен ломать сверку -- это уведомление, а не
        критическая операция.

        price -- цена ИСПОЛНЕНИЯ именно этого шага, если вызывающий её знает напрямую
        (paper_broker.py -- эмулятор сам её только что зафиксировал). Если None (реальный
        брокерский синк, sync_account_fb.py -- там нет цены конкретной сделки, только
        снэпшот позиции) -- откат на старую эвристику (см. BACKLOG.md, найденный баг
        2026-08-18): order_price по привязанному ордеру, иначе assets.avg_price. Для ПРОДАЖИ
        эта эвристика систематически ошибается -- avg_price намеренно НЕ меняется при частичном
        выходе (себестоимость остатка), а не цена только что состоявшейся продажи -- отсюда и
        живой баг (TPR продан по $133.90, уведомление показало $129.62, старую цену покупки).
        """
        try:
            row = self.db.execute_row("""
                SELECT op.current_step, op.pending_broker_order_id,
                       t.symbol, s.strategy_name, port.name AS portfolio_name,
                       u.telegram_id, o.p AS order_price, a.avg_price
                FROM public.order_pipelines op
                JOIN public.tickers t ON op.ticker_id = t.id
                JOIN public.strategies s ON op.strategy_id = s.id
                JOIN public.portfolios port ON op.portfolio_id = port.id
                JOIN public.users u ON port.owner_id = u.id
                LEFT JOIN public.orders o ON o.broker_order_id = op.pending_broker_order_id
                LEFT JOIN public.assets a ON a.portfolio_id = op.portfolio_id AND a.listing_id = op.listing_id
                WHERE op.id = %s;
            """, (pipeline_id,))
            if not row or not row.get("telegram_id"):
                return

            if price is not None:
                fallback_price = price
            else:
                fallback_price = row.get("order_price") if row.get("order_price") is not None else row.get("avg_price")
            price_str = f"{float(fallback_price):.2f}" if fallback_price is not None else "?"
            qty_str = f"{abs(float(matched_qty)):.0f}"
            is_buy = float(matched_qty) > 0
            verb_done = "Куплено" if is_buy else "Продано"

            if action == 'COMPLETE_PIPELINE':
                text = (
                    f"✅ План по *{row['symbol']}* ({row['strategy_name']}, портфель {row['portfolio_name']}) "
                    f"завершён: {verb_done} {qty_str} шт по {price_str}."
                )
            else:
                text = (
                    f"✅ Шаг {int(row['current_step'])} плана по *{row['symbol']}* ({row['strategy_name']}, "
                    f"портфель {row['portfolio_name']}) исполнен: {verb_done} {qty_str} шт по {price_str}. "
                    f"Следующий шаг: {int(row['current_step']) + 1}."
                )

            token = os.getenv("TELEGRAM_TOKEN")
            if not token:
                logging.warning("⚠️ [UPort Стратегии]: TELEGRAM_TOKEN отсутствует -- уведомление о шаге не отправлено.")
                return

            requests.post(
                TELEGRAM_API_URL.format(token=token),
                json={"chat_id": int(row["telegram_id"]), "text": text, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception as notify_err:
            logging.error(f"⚠️ [UPort Стратегии]: Не удалось отправить уведомление о шаге плана (pipeline_id={pipeline_id}): {notify_err}")

    def _update_pipeline_status(self, pipeline_id: int, action: str):
        """
        Управляет жизненным циклом конвейера ордеров.
        Закрывает идею целиком или переключает шаг лесенки вперед.
        В обоих случаях снимает pending_broker_order_id -- отработавший ордер больше не ожидается,
        следующий шаг (если есть) должен быть привязан заново через bot_handlers/order_pipelines.py.
        """
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")

        if action == 'COMPLETE_PIPELINE':
            # Цель достигнута полностью, закрываем конвейер. step_ready_notified_at
            # сбрасывается той же логикой, что и у NEXT_STEP ниже (см. её комментарий) --
            # без сброса завершённый План продолжал бы вечно показываться в дайджесте
            # строкой "🪜 условие выполнено" (живой баг, найден пользователем на живом
            # П10 -- дублирующиеся строки OPEN, Claude/BACKLOG.md, тот же корень для
            # обоих действий, не только для NEXT_STEP).
            sql = """
                UPDATE public.order_pipelines
                SET pipeline_status = 'COMPLETED',
                    pending_broker_order_id = NULL,
                    step_ready_notified_at = NULL,
                    updated_at = %s
                WHERE id = %s;
            """
            self.db.execute_query(sql, (system_now, pipeline_id))

        elif action == 'NEXT_STEP':
            # Шаг лесенки совпал, переключаем конвейер на следующий уровень и активируем его статус.
            # step_ready_notified_at сбрасывается -- флаг относится к ТЕКУЩЕМУ шагу (LadderStepWatcher
            # проверяет "уже уведомляли" перед КАЖДЫМ шагом отдельно), без сброса он остался бы
            # true с предыдущего шага и молча подавил бы уведомление о готовности следующего (найдено
            # при тестировании эмулятора брокера -- см. Claude/BACKLOG.md №117/122, актуально и для
            # реальных портфелей с многошаговой лесенкой, не только для бумажного).
            sql = """
                UPDATE public.order_pipelines
                SET current_step = current_step + 1,
                    pipeline_status = 'ACTIVE',
                    pending_broker_order_id = NULL,
                    step_ready_notified_at = NULL,
                    updated_at = %s
                WHERE id = %s;
            """
            self.db.execute_query(sql, (system_now, pipeline_id))
