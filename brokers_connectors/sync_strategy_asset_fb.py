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
        sql = f"""
            SELECT quantity 
            FROM public.assets 
            WHERE portfolio_id = {int(portfolio_id)} 
              AND listing_id = {int(listing_id)};
        """
        # Используем безопасный метод одной строки без скобок
        row = self.db.execute_row(sql)
        if 'quantity' in row:
            return float(row['quantity'])

        # Если записи в assets нет (абсолютно новая бумага) — возвращаем 0.0
        return 0.0

    def distribute_asset_delta(self, portfolio_id: int, listing_id: int, ticker_id: int, old_qty: float, new_qty: float):
        """
        Метод 2: Главный диспетчер распределения. Вычисляется дельта движения акций
        (плюс при покупке, минус при продаже) и зачисляется в нужную стратегию.
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

        if action in ('NEXT_STEP', 'COMPLETE_PIPELINE'):
            logging.info(f"✅ [UPort Стратегии]: Шаг плана (стратегия ID: {target_strategy_id}) засчитан -- {action}")
            # Уведомление -- ДО обновления статуса конвейера, пока current_step/pending_broker_order_id
            # ещё не сброшены (см. Claude/09_pipeline_reconciliation.md, реального "срочного канала" не
            # требуется -- вызов сидит на уже существующем реальном времени WebSocket-триггера ФБ)
            self._notify_step_filled(pipeline_id, action, matched_qty)
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
        condition = "pending_broker_order_id IS NOT NULL" if require_linked_order else "pending_broker_order_id IS NULL"
        sql = f"""
            SELECT id, strategy_id, current_step, target_quantity
            FROM public.order_pipelines
            WHERE portfolio_id = {int(portfolio_id)}
              AND ticker_id = {int(ticker_id)}
              AND pipeline_status IN ('PENDING', 'ACTIVE')
              AND {condition};
        """
        pipes = self.db.execute_query(sql)
        pipes = pipes if isinstance(pipes, list) else ([pipes] if pipes else [])

        if len(pipes) == 0:
            return None
        if len(pipes) > 1:
            kind = "с привязанным ордером" if require_linked_order else "без привязанного ордера ('План входа')"
            logging.warning(f"⚠️ [UPort Стратегии]: {len(pipes)} планов {kind} одновременно ждут ticker_id {ticker_id} в портфеле {portfolio_id} -- дельта не может быть однозначно сопоставлена.")
            return None
        return pipes[0]

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

        # Ни одного плана вообще -- ни привязанного, ни "голого" -- либо план не заведён,
        # либо между шагами (пользователь ещё не привязал следующий ордер).
        if pipe is None:
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
        sql_tactic = f"""
            SELECT budget_share_pct
            FROM public.strategy_tactics
            WHERE strategy_id = {strat_id}
              AND step_number = {curr_step};
        """
        tactic = self.db.execute_row(sql_tactic)
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
        sql = f"""
            SELECT s.id
            FROM public.strategies s
            JOIN public.strategy_templates st ON s.template_id = st.id
            WHERE s.portfolio_id = {int(portfolio_id)}
              AND st.system_key = 'UNALLOCATED';
        """
        # Безопасно забираем одну строку буфера
        row = self.db.execute_row(sql)
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
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        
        # Получаем ID связи из assets, так как таблица strategy_assets жестко привязана к общему котлу
        sql_asset = f"SELECT id FROM public.assets WHERE portfolio_id = {int(portfolio_id)} AND listing_id = {int(listing_id)};"
        asset_row = self.db.execute_row(sql_asset)
        
        if not asset_row:
            logging.error(f"🚨 [UPort]: Не удалось найти asset_id для листинга {listing_id} портфеля {portfolio_id}")
            return

        asset_id = int(asset_row['id'])

        # Магия сквозной математики: просто прибавляем дельту (неважно, +5 или -5)
        sql_upsert = f"""
            INSERT INTO public.strategy_assets (asset_id, strategy_id, allocated_quantity, last_updated_at)
            VALUES ({asset_id}, {int(strategy_id)}, {delta}, '{system_now}')
            ON CONFLICT (asset_id, strategy_id) 
            DO UPDATE SET 
                allocated_quantity = public.strategy_assets.allocated_quantity + EXCLUDED.allocated_quantity,
                last_updated_at = EXCLUDED.last_updated_at;
        """
        self.db.execute_query(sql_upsert)

    def _notify_step_filled(self, pipeline_id: int, action: str, matched_qty: float):
        """
        Присылает владельцу портфеля сообщение в Telegram о том, что шаг плана исполнен
        и распределён в стратегию. Вызывается ДО _update_pipeline_status, пока current_step
        и pending_broker_order_id ещё не сброшены. Сбой отправки не должен ломать сверку --
        это уведомление, а не критическая операция.
        """
        try:
            row = self.db.execute_row(f"""
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
                WHERE op.id = {int(pipeline_id)};
            """)
            if not row or not row.get("telegram_id"):
                return

            # Цена ордера есть только у шага, привязанного вручную (pending_broker_order_id) --
            # у "голого" плана ("План входа", см. Claude/11_asset_lifecycle_and_plan.md), пойманного
            # автоматически при покупке рынком, order_price всегда NULL. Реальная цена в этом
            # случае уже лежит в assets.avg_price (только что записана этим же циклом синхронизации).
            fallback_price = row.get("order_price") if row.get("order_price") is not None else row.get("avg_price")
            price_str = f"{float(fallback_price):.2f}" if fallback_price is not None else "?"
            qty_str = f"{abs(float(matched_qty)):.0f}"

            if action == 'COMPLETE_PIPELINE':
                text = (
                    f"✅ План по *{row['symbol']}* ({row['strategy_name']}, портфель {row['portfolio_name']}) "
                    f"завершён: {qty_str} шт по {price_str}. Распределено в стратегию."
                )
            else:
                text = (
                    f"✅ Шаг {int(row['current_step'])} плана по *{row['symbol']}* ({row['strategy_name']}, "
                    f"портфель {row['portfolio_name']}) исполнен: {qty_str} шт по {price_str}. "
                    f"Распределено в стратегию. Следующий шаг: {int(row['current_step']) + 1}."
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
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

        if action == 'COMPLETE_PIPELINE':
            # Цель достигнута полностью, закрываем конвейер
            sql = f"""
                UPDATE public.order_pipelines
                SET pipeline_status = 'COMPLETED',
                    pending_broker_order_id = NULL,
                    updated_at = '{system_now}'
                WHERE id = {int(pipeline_id)};
            """
            self.db.execute_query(sql)

        elif action == 'NEXT_STEP':
            # Шаг лесенки совпал, переключаем конвейер на следующий уровень и активируем его статус
            sql = f"""
                UPDATE public.order_pipelines
                SET current_step = current_step + 1,
                    pipeline_status = 'ACTIVE',
                    pending_broker_order_id = NULL,
                    updated_at = '{system_now}'
                WHERE id = {int(pipeline_id)};
            """
            self.db.execute_query(sql)
