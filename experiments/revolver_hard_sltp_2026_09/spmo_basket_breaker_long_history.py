"""
Тот же circuit breaker (breadth + агрегатная просадка корзины, полный выход),
что и spmo_basket_circuit_breaker_sweep.py, но на расширенной истории
2014-2026 (pull_long_history_2014_2026.py) -- 22 периода ребалансировки вместо
5, статистика уже осмысленная, не "что случилось в конкретных 3-5 окнах".

Точки ребалансировки -- 3-я пятница марта/сентября 2015-2026 (подтверждённый
календарь SPMO с сайта Invesco), корзина на каждую точку пересчитывается
point-in-time (без заглядывания вперёд) -- та же momentum_score/get_top_n
логика, что в spmo_top20_tp_threshold_sweep.py.
"""
from pathlib import Path

import pandas as pd

from spmo_top20_tp_threshold_sweep import load_frames, get_top_n
from spmo_basket_circuit_breaker_sweep import (
    simulate_basket_period, BREADTH_THRESHOLDS, AGG_DD_THRESHOLDS,
)

CACHE_DIR = Path(__file__).resolve().parent / '_cache'
LONG_CACHE = 'signal_frames_long_2014_2026.pkl'
DATA_END = pd.Timestamp('2026-09-20')


def third_friday(year, month):
    d = pd.Timestamp(year=year, month=month, day=1)
    fridays = pd.date_range(d, d + pd.DateOffset(months=1) - pd.Timedelta(days=1), freq='W-FRI')
    return fridays[2]


def build_rebalance_dates():
    dates = []
    for y in range(2015, 2027):
        for m in (3, 9):
            dates.append(third_friday(y, m))
    return [d for d in dates if pd.Timestamp('2015-08-01') <= d <= DATA_END]


def main():
    frames = load_frames(LONG_CACHE)
    reb_dates = build_rebalance_dates()
    period_bounds = list(zip(reb_dates, reb_dates[1:] + [DATA_END]))
    print(f"Периодов ребалансировки: {len(period_bounds)} ({reb_dates[0].date()} -> {DATA_END.date()})")

    baskets = {}
    for reb in reb_dates[:-1]:  # последняя дата -- только конец последнего периода, корзина не нужна
        baskets[reb] = get_top_n(frames, reb)
    for reb, basket in list(baskets.items())[:3]:
        print(f"  топ-20 на {reb.date()}: {', '.join(basket[:6])}...")

    rows = []
    for breadth_thr in BREADTH_THRESHOLDS:
        for agg_dd_thr in AGG_DD_THRESHOLDS:
            period_results = []
            for start, end in period_bounds:
                if start not in baskets:
                    continue
                r = simulate_basket_period(frames, baskets[start], start, end, breadth_thr, agg_dd_thr)
                if r is not None:
                    period_results.append(r)

            pr = pd.DataFrame(period_results)
            if pr.empty:
                continue

            n_periods = len(pr)
            n_triggered = int(pr['triggered'].sum())
            avg_TP = pr['TP_ret'].mean()
            avg_noTP = pr['noTP_ret'].mean()
            triggered = pr[pr['triggered']]
            avg_post_exit_move = triggered['post_exit_move'].mean() if len(triggered) else float('nan')
            # доля периодов, где TP-путь оказался ЛУЧШЕ noTP (win rate самого механизма)
            win_rate = (pr['TP_ret'] > pr['noTP_ret']).mean()

            rows.append(dict(
                breadth_thr=breadth_thr, agg_dd_thr=agg_dd_thr,
                n_periods=n_periods, n_triggered=n_triggered,
                avg_TP_ret=avg_TP, avg_noTP_ret=avg_noTP, delta=avg_TP - avg_noTP,
                win_rate=win_rate, avg_post_exit_move=avg_post_exit_move,
            ))

    full = pd.DataFrame(rows)
    pd.set_option('display.width', 200)
    pd.set_option('display.max_columns', 20)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    print("\n=== СВОДНАЯ ТАБЛИЦА (circuit breaker, длинная история 2015-2026) ===")
    print(full.to_string(index=False))

    out_path = CACHE_DIR / 'spmo_basket_breaker_long_history_results.csv'
    full.to_csv(out_path, index=False)
    print(f"\nСохранено: {out_path}")


if __name__ == '__main__':
    main()
