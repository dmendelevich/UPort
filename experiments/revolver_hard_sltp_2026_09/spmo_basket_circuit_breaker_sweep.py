"""
Circuit breaker НА УРОВНЕ КОРЗИНЫ (не отдельной бумаги) -- продолжение темы
после провала per-position trailing-TP (spmo_top20_tp_threshold_sweep.py:
на бычьем эпизоде TP резал хвост победителей на любом пороге вплоть до -30%,
идиосинкратический шум одной бумаги оказался слишком велик).

Идея: гасим идиосинкратический шум усреднением по 20 позициям, ловим только
СОГЛАСОВАННОЕ движение всей темы -- двумя метриками одновременно:

  - breadth   -- доля из 20 позиций, одновременно просевших >= PER_STOCK_DD
                 от СВОЕГО пика (ранний / чувствительный сигнал -- ротация темы).
  - agg_dd    -- просадка агрегированной (equal-weight) стоимости всей корзины
                 от ЕЁ пика (более поздний, но "денежный" сигнал -- сколько
                 реально теряем).

Тестируемое действие при срабатывании -- ПОЛНЫЙ ВЫХОД из корзины (обе метрики
одновременно превышают порог -> продаём все 20 позиций, ждём следующую
ребалансировку). Те же два независимых эпизода и те же point-in-time
восстановленные корзины, что и в TP-свипе -- переиспользуем функции оттуда.
"""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from spmo_top20_tp_threshold_sweep import (
    CACHE_DIR, EPISODES, load_frames, get_top_n,
)

PER_STOCK_DD = 0.15  # тот же порядок величины, что и середина flat-свипа TP
BREADTH_THRESHOLDS = [0.40, 0.50, 0.60, 0.70]
AGG_DD_THRESHOLDS = [0.05, 0.08, 0.10, 0.15]
FORWARD_WINDOW = 20


def simulate_basket_period(frames, basket, start_ts, end_ts,
                            breadth_thr, agg_dd_thr, per_stock_dd=PER_STOCK_DD,
                            forward_window=FORWARD_WINDOW):
    members = []
    for sym in basket:
        df = frames.get(sym)
        if df is None:
            continue
        idx = df.index
        window_idx = idx[(idx >= start_ts) & (idx <= end_ts)]
        if len(window_idx) < 2:
            continue
        members.append((sym, df, window_idx))

    if not members:
        return None

    # общий календарь -- пересечение дат по всем членам корзины
    common_dates = None
    for sym, df, window_idx in members:
        common_dates = window_idx if common_dates is None else common_dates.intersection(window_idx)
    common_dates = common_dates.sort_values()
    if len(common_dates) < 2:
        return None

    entry_price = {sym: df.loc[common_dates[0], 'close'] for sym, df, _ in members}
    peak_price = dict(entry_price)
    agg_peak = 1.0
    triggered = False
    exit_date, exit_nav = None, None

    for d in common_dates:
        below = 0
        ratios = []
        for sym, df, _ in members:
            close = df.loc[d, 'close']
            peak_price[sym] = max(peak_price[sym], close)
            dd = 1 - close / peak_price[sym]
            if dd >= per_stock_dd:
                below += 1
            ratios.append(close / entry_price[sym])

        breadth = below / len(members)
        agg_nav = float(np.mean(ratios))
        agg_peak = max(agg_peak, agg_nav)
        agg_dd = 1 - agg_nav / agg_peak

        if breadth >= breadth_thr and agg_dd >= agg_dd_thr:
            triggered, exit_date, exit_nav = True, d, agg_nav
            break

    last_nav = float(np.mean([df.loc[common_dates[-1], 'close'] / entry_price[sym] for sym, df, _ in members]))
    noTP_ret = last_nav - 1

    if triggered:
        TP_ret = exit_nav - 1
        after_navs = []
        for sym, df, _ in members:
            idx = df.index
            after_idx = idx[idx > exit_date][:forward_window]
            if len(after_idx) == 0:
                continue
            after_navs.append(df.loc[after_idx[-1], 'close'] / entry_price[sym])
        post_exit_move = (float(np.mean(after_navs)) / exit_nav - 1) if after_navs else np.nan
    else:
        TP_ret = noTP_ret
        post_exit_move = np.nan

    return dict(triggered=triggered, TP_ret=TP_ret, noTP_ret=noTP_ret, post_exit_move=post_exit_move)


def run_episode(name, cfg):
    frames = load_frames(cfg['cache'])
    reb_dates = [pd.Timestamp(d) for d in cfg['rebalance_dates']]
    data_end = pd.Timestamp(cfg['data_end'])
    period_bounds = list(zip(reb_dates, reb_dates[1:] + [data_end]))

    baskets = {reb: get_top_n(frames, reb) for reb in reb_dates}

    rows = []
    for breadth_thr in BREADTH_THRESHOLDS:
        for agg_dd_thr in AGG_DD_THRESHOLDS:
            period_results = []
            for start, end in period_bounds:
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
            avg_post_exit_move = triggered['post_exit_move'].mean() if len(triggered) else np.nan

            rows.append(dict(
                episode=name, breadth_thr=breadth_thr, agg_dd_thr=agg_dd_thr,
                n_periods=n_periods, n_triggered=n_triggered,
                avg_TP_ret=avg_TP, avg_noTP_ret=avg_noTP, delta=avg_TP - avg_noTP,
                avg_post_exit_move=avg_post_exit_move,
            ))

    return pd.DataFrame(rows)


def main():
    all_results = []
    for name, cfg in EPISODES.items():
        print(f"\n=== Эпизод: {name} ===")
        res = run_episode(name, cfg)
        all_results.append(res)

    full = pd.concat(all_results, ignore_index=True)
    pd.set_option('display.width', 200)
    pd.set_option('display.max_columns', 20)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    print("\n\n=== СВОДНАЯ ТАБЛИЦА (circuit breaker на корзину) ===")
    print(full.to_string(index=False))

    out_path = Path(__file__).resolve().parent / '_cache' / 'spmo_basket_breaker_sweep_results.csv'
    full.to_csv(out_path, index=False)
    print(f"\nСохранено: {out_path}")


if __name__ == '__main__':
    main()
