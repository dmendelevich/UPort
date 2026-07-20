import os
import json
import logging
from datetime import datetime, timezone

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

        # Шаг 1: Ищем, какая стратегия ожидает эту дельту (целиком или по шагу лесенки)
        target_strategy_id, pipeline_id, step_to_update = self._find_target_strategy(portfolio_id, ticker_id, delta)
        
        # Шаг 2: Если план не найден, дельта летит в "Нераспределенные" (Шаблон №5)
        if target_strategy_id is None:
            target_strategy_id = self._get_unallocated_strategy_id(portfolio_id)
            logging.info(f"ℹ️ [UPort Стратегии]: Дельта {delta} по ticker_id {ticker_id} ушла в 'Нераспределенные'")
        else:
            logging.info(f"✅ [UPort Стратегии]: Дельта {delta} совпала с планом! Стратегия ID: {target_strategy_id}")

        # Шаг 3: Фиксируем изменения в таблице strategy_assets и обновляем конвейер ордеров
        self._apply_strategy_balance(portfolio_id, listing_id, target_strategy_id, delta)
        
        if pipeline_id:
            self._update_pipeline_status(pipeline_id, step_to_update)

    def _find_target_strategy(self, portfolio_id: int, ticker_id: int, delta: float):
        """
        Внутренний аналитический узел. Ищет активный конвейер в order_pipelines.
        🔥 УЛЬТРА-ФИКС: Строго целые числа (int) для дельты + Предохранитель минимального шага лесенки!
        """
        # Жестко отсекаем дробную часть у прилетевшей от брокера дельты
        clean_delta = int(delta)
        
        # Если движение составило меньше 1 акции (например, дробный хвост), игнорируем
        if clean_delta == 0:
            return None, None, None

        sql = f"""
            SELECT id, strategy_id, current_step, target_quantity 
            FROM public.order_pipelines 
            WHERE portfolio_id = {int(portfolio_id)} 
              AND ticker_id = {int(ticker_id)} 
              AND pipeline_status IN ('PENDING', 'ACTIVE');
        """
        # Безопасно забираем верхний подходящий конвейер плана
        pipe = self.db.execute_row(sql)
        if not pipe:
            return None, None, None

        pipe_id = int(pipe['id'])
        strat_id = int(pipe['strategy_id'])
        curr_step = int(pipe['current_step'])
        target_qty = int(float(pipe['target_quantity'])) # Приводим цель к целому числу
        
        # Проверка 1: Полное совпадение дельты со всей целью идеи
        if clean_delta == target_qty:
            return strat_id, pipe_id, 'COMPLETE_PIPELINE'

        # Проверка 2: Совпадение с текущим шагом лесенки (через strategy_tactics)
        sql_tactic = f"""
            SELECT budget_share_pct 
            FROM public.strategy_tactics 
            WHERE strategy_id = {strat_id} 
              AND step_number = {curr_step};
        """
        # Достаем параметры конкретного шага стратегии в один клик
        tactic = self.db.execute_row(sql_tactic)
        if tactic:
            step_share = float(tactic['budget_share_pct']) / 100.0            

            # 🔥 ПРЕДОХРАНИТЕЛЬ UPORT: Высчитываем округленный объем шага, но СТРОГО запрещаем ему быть меньше 1 целой акции!
            expected_step_qty = max(1, round(target_qty * step_share))
            
            # Теперь математика лесенки (3 акции на 3 шага) даст ровно: 1 == 1
            if clean_delta == expected_step_qty:
                return strat_id, pipe_id, 'NEXT_STEP'

        return None, None, None

    def _get_unallocated_strategy_id(self, portfolio_id: int) -> int:
        """
        Быстро находит ID виртуальной стратегии "Нераспределенные" (Шаблон №5) для данного портфеля.
        """
        sql = f"""
            SELECT id FROM public.strategies 
            WHERE portfolio_id = {int(portfolio_id)} 
              AND strategy_name = 'Нераспределенные' 
               OR strategy_name = 'Неопределенная стратегия';
        """
        # Безопасно забираем одну строку буфера
        row = self.db.execute_row(sql)
        if row:
            return int(row['id'])
        
        # Жесткий аварийный fallback, если стратегии-буфера вдруг нет в базе
        raise ValueError(f"Критическая ошибка: В портфеле {portfolio_id} отсутствует буферная стратегия 'Нераспределенные'!")

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

    def _update_pipeline_status(self, pipeline_id: int, action: str):
        """
        Управляет жизненным циклом конвейера ордеров. 
        Закрывает идею целиком или переключает шаг лесенки вперед.
        """
        system_now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

        if action == 'COMPLETE_PIPELINE':
            # Цель достигнута полностью, закрываем конвейер
            sql = f"""
                UPDATE public.order_pipelines 
                SET pipeline_status = 'COMPLETED', 
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
                    updated_at = '{system_now}' 
                WHERE id = {int(pipeline_id)};
            """
            self.db.execute_query(sql)
