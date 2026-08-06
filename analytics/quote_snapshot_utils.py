import logging

# Стандарт времени UPort (см. ARCHITECTURE/Time.md): TIMESTAMP(0) WITHOUT TIME ZONE, UTC.


def record_quote_snapshot(db_instance, listing_id: int, price: float, buffer_size: int):
    """
    Кольцевой буфер последних котировок ОДНОГО листинга (см. Claude/05_strategy_screen_and_kubiki.md,
    PriceMoveWatcher). Пишет новую строку, затем удаляет самые старые сверх buffer_size -- Postgres
    не имеет встроенного механизма кольцевого буфера, поддерживаем его вручную.
    """
    sql_insert = """
        INSERT INTO public.quote_snapshots (listing_id, price, recorded_at)
        VALUES (%s, %s, (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0));
    """
    db_instance.execute_query(sql_insert, (listing_id, price))

    sql_prune = """
        DELETE FROM public.quote_snapshots
        WHERE listing_id = %s AND id NOT IN (
            SELECT id FROM public.quote_snapshots
            WHERE listing_id = %s
            ORDER BY recorded_at DESC, id DESC
            LIMIT %s
        );
    """
    db_instance.execute_query(sql_prune, (listing_id, listing_id, buffer_size))


def compute_price_move(db_instance, listing_id: int, current_price: float, window_minutes: int):
    """
    % изменения текущей цены относительно ближайшей по времени котировки НЕ БЛИЖЕ
    window_minutes назад -- ищем реальный интервал времени по recorded_at, а не N-ю
    по счёту запись (устойчиво к изменению частоты синка котировок).

    Возвращает (pct_move, актуальное_число_минут) или None, если в буфере ещё нет
    записи старше window_minutes (недостаточно истории для этого окна).
    """
    # Считаем "сколько минут назад" в SQL (EXTRACT), а не вычитанием datetime в Python --
    # ответ шлюза СУБД приходит как обычный JSON (даты -- строки), сравнение сразу в
    # SQL проще и надёжнее (см. тот же приём в Блоке 1, format_position_financials).
    ref_row = db_instance.execute_row("""
        SELECT price,
               EXTRACT(EPOCH FROM ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - recorded_at))::int / 60 AS minutes_ago
        FROM public.quote_snapshots
        WHERE listing_id = %s
          AND recorded_at <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::timestamp(0) - (%s * INTERVAL '1 minute')
        ORDER BY recorded_at DESC
        LIMIT 1;
    """, (listing_id, window_minutes))
    if not ref_row:
        return None

    ref_price = float(ref_row["price"])
    if ref_price <= 0:
        return None

    actual_minutes = int(ref_row["minutes_ago"])
    pct_move = ((float(current_price) / ref_price) - 1) * 100
    return pct_move, actual_minutes
