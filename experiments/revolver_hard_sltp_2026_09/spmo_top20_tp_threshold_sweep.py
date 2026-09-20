"""
Sweep порогов trailing-TP для гипотетической топ-20 momentum-корзины (SPMO-style
отбор: risk-adjusted momentum = avg(12-1мес, 6-1мес доходность) / волатильность).

Цель -- проверить сам вопрос темы: режет ли trailing-TP хвост победителей
(на чём и делается вся momentum-доходность) или ловит реальные развороты.

Point-in-time восстановление топ-20 на КАЖДУЮ дату ребалансировки отдельно
(3-я пятница марта/сентября, подтверждено с сайта Invesco) -- НЕ бэктест
сегодняшнего топ-20 SPMO назад во времени (это был бы survivorship bias:
сегодняшние лидеры вроде MU/NVDA/AVGO уже известны как победители задним
числом, в 2022-м моментум-лидерами были другие бумаги).

Два независимых эпизода из уже готового кэша Revolver-экспериментов (не тянем
новые данные): бычий 2024-2026 и медвежий 2022 (Claude/BACKLOG.md, правило
"находка засчитывается только если подтверждена на 2+ независимых периодах").

Trailing-стоп формула K x volatility -- та же, что в capital_protection_watcher.py
/ k_vol_exit_long_run.py (peak * (1 - K * vol_pct/100)), для сравнимости с уже
проверенной для Revolver логикой.
"""
import sys
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent / '_cache'

TOP_N = 20
FLAT_THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30]
K_THRESHOLDS = [3, 4, 5, 6]
FORWARD_WINDOW = 20  # торговых дней после триггера -- смотрим, что было дальше

EPISODES = {
    'bull_2025_2026': {
        'cache': 'signal_frames_combined_sleeves.pkl',
        'rebalance_dates': ['2025-03-21', '2025-09-19', '2026-03-20'],
        'data_end': '2026-09-04',
    },
    'bear_2022': {
        'cache': 'signal_frames_combined_sleeves_2022.pkl',
        'rebalance_dates': ['2022-03-18', '2022-09-16'],
        'data_end': '2022-10-14',
    },
}


def load_frames(fn):
    with open(CACHE_DIR / fn, 'rb') as f:
        return pickle.load(f)


def price_months_back(df, idx, asof_ts, months):
    target = asof_ts - pd.DateOffset(months=months)
    prior_idx = idx[idx <= target]
    if len(prior_idx) == 0:
        return None
    return df.loc[prior_idx[-1], 'close']


def momentum_score(df, asof_ts):
    idx = df.index
    prior = idx[idx <= asof_ts]
    if len(prior) == 0:
        return None
    asof_ts_actual = prior[-1]

    p1 = price_months_back(df, idx, asof_ts_actual, 1)
    p6 = price_months_back(df, idx, asof_ts_actual, 6)
    p12 = price_months_back(df, idx, asof_ts_actual, 12)
    if p1 is None or p6 is None or p12 is None or p6 == 0 or p12 == 0:
        return None

    hist = df.loc[idx <= asof_ts_actual].tail(252)
    if len(hist) < 200:
        return None
    weekly_vol = hist['close'].pct_change().std() * np.sqrt(5)
    if not weekly_vol or np.isnan(weekly_vol) or weekly_vol == 0:
        return None

    ret_12_1 = p1 / p12 - 1
    ret_6_1 = p1 / p6 - 1
    return (ret_12_1 / weekly_vol + ret_6_1 / weekly_vol) / 2


def get_top_n(frames, asof_ts, n=TOP_N):
    scores = {}
    for sym, df in frames.items():
        s = momentum_score(df, asof_ts)
        if s is not None:
            scores[sym] = s
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [sym for sym, _ in ranked[:n]]


def simulate_position(df, start_ts, end_ts, threshold_type, threshold_val):
    idx = df.index
    window = df.loc[(idx >= start_ts) & (idx <= end_ts)]
    if len(window) < 2:
        return None

    entry_price = window['close'].iloc[0]
    peak = entry_price
    triggered = False
    exit_ts, exit_price = None, None

    for d, row in window.iterrows():
        hi = row['high'] if not np.isnan(row['high']) else row['close']
        peak = max(peak, hi)

        if threshold_type == 'flat':
            trigger_price = peak * (1 - threshold_val)
        else:
            vol = row['vol_pct']
            if np.isnan(vol) or vol <= 0:
                continue
            trigger_price = peak * (1 - threshold_val * vol / 100.0)

        lo = row['low'] if not np.isnan(row['low']) else row['close']
        if lo <= trigger_price:
            triggered, exit_ts, exit_price = True, d, trigger_price
            break

    noTP_ret = window['close'].iloc[-1] / entry_price - 1

    if triggered:
        TP_ret = exit_price / entry_price - 1
        after = df.loc[idx > exit_ts].head(FORWARD_WINDOW)
        post_exit_move = (after['close'].iloc[-1] / exit_price - 1) if len(after) else np.nan
    else:
        TP_ret = noTP_ret
        post_exit_move = np.nan

    return dict(triggered=triggered, TP_ret=TP_ret, noTP_ret=noTP_ret, post_exit_move=post_exit_move)


def run_episode(name, cfg):
    frames = load_frames(cfg['cache'])
    reb_dates = [pd.Timestamp(d) for d in cfg['rebalance_dates']]
    data_end = pd.Timestamp(cfg['data_end'])
    period_bounds = list(zip(reb_dates, reb_dates[1:] + [data_end]))

    baskets = {}
    for reb in reb_dates:
        basket = get_top_n(frames, reb)
        baskets[reb] = basket
        print(f"  [{name}] топ-20 на {reb.date()}: {', '.join(basket[:8])}...")

    all_thresholds = [('flat', t) for t in FLAT_THRESHOLDS] + [('kvol', k) for k in K_THRESHOLDS]
    rows = []

    for ttype, tval in all_thresholds:
        period_results = []
        for start, end in period_bounds:
            basket = baskets[start]
            for sym in basket:
                df = frames.get(sym)
                if df is None:
                    continue
                r = simulate_position(df, start, end, ttype, tval)
                if r is not None:
                    r['symbol'] = sym
                    r['period'] = f"{start.date()}->{end.date()}"
                    period_results.append(r)

        pr = pd.DataFrame(period_results)
        if pr.empty:
            continue

        trigger_rate = pr['triggered'].mean()
        avg_TP = pr['TP_ret'].mean()
        avg_noTP = pr['noTP_ret'].mean()

        triggered = pr[pr['triggered']]
        bounce_rate = (triggered['post_exit_move'] > 0).mean() if len(triggered) else np.nan
        avg_post_exit_move = triggered['post_exit_move'].mean() if len(triggered) else np.nan

        # "хвост победителей": верхняя четверть по noTP_ret -- топ-перформеры периода
        q75 = pr['noTP_ret'].quantile(0.75)
        winners = pr[pr['noTP_ret'] >= q75]
        winner_clip_rate = winners['triggered'].mean() if len(winners) else np.nan
        winner_give_back = (winners['TP_ret'] - winners['noTP_ret']).mean() if len(winners) else np.nan

        rows.append(dict(
            episode=name, threshold_type=ttype, threshold_val=tval,
            n=len(pr), trigger_rate=trigger_rate,
            avg_TP_ret=avg_TP, avg_noTP_ret=avg_noTP, delta=avg_TP - avg_noTP,
            bounce_rate=bounce_rate, avg_post_exit_move=avg_post_exit_move,
            winner_clip_rate=winner_clip_rate, winner_give_back=winner_give_back,
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

    print("\n\n=== СВОДНАЯ ТАБЛИЦА ===")
    print(full.to_string(index=False))

    out_path = Path(__file__).resolve().parent / '_cache' / 'spmo_tp_sweep_results.csv'
    full.to_csv(out_path, index=False)
    print(f"\nСохранено: {out_path}")


if __name__ == '__main__':
    main()
