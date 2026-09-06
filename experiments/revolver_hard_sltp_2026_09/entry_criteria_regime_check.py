"""
Проверка гипотезы пользователя (2026-09-06, продолжение серии #167/#168):
не выход/стратегия, а КРИТЕРИИ ОТБОРА КАНДИДАТОВ должны быть строже в
просевшие периоды -- один и тот же экран (RSI<45 + др.) даёт winrate 56-58%
на бычьих периодах и 38-44% на просевших, при ОДИНАКОВОМ выходе (широкий
10%/10%) -- проблема в качестве кандидата на входе, не в правиле выхода.

Прогоняет 4 уже использованных независимых периода (2023 бык, 2022 короткий
обвал, фев-апр 2025 просадка, апр2025-фев2026 бык) с ДВУМЯ порогами RSI
(45 -- нынешний, 35 -- строже, глубже перепроданность) при фиксированном
широком выходе (10%/10%) -- смотрим, сокращается ли разрыв winrate между
бычьими и просевшими периодами.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals_default = {r['symbol']: r for r in json.load(f)}

EPISODES = [
    ('2023 бык (янв-июн)', 'signal_frames_2023.pkl', date(2023, 1, 3), date(2023, 6, 1)),
    ('2022 короткий обвал (янв-мар)', 'signal_frames_2022.pkl', date(2022, 1, 3), date(2022, 3, 1)),
    ('фев-апр 2025 просадка', 'signal_frames_2025_feb_apr.pkl', date(2025, 2, 1), date(2025, 4, 21)),
    ('апр2025-фев2026 бык', 'signal_frames_2025_2026_bull.pkl', date(2025, 4, 21), date(2026, 2, 20)),
]

bt.SL_PCT, bt.TP_PCT = 10.0, 10.0


def run_episode(frames_file, start, end, rsi_limit):
    with open(CACHE_DIR / frames_file, 'rb') as f:
        frames = pickle.load(f)
    bt.LIMIT_RSI = rsi_limit

    checkpoints = []
    d = start
    while d <= end:
        checkpoints.append(d)
        d += timedelta(days=bt.RESCAN_DAYS)

    slot_value = [bt.SLOT_USD] * bt.N_SLOTS
    trades = []
    for cp in checkpoints:
        chosen = bt.screen_at_date(cp, fundamentals_default, frames)[:bt.N_SLOTS]
        for i in range(bt.N_SLOTS):
            if i >= len(chosen):
                continue
            sym, rank, price = chosen[i]
            exit_price, exit_date, reason = bt.simulate_slot(sym, cp, price, frames)
            gross_pct = (exit_price - price) / price * 100.0
            net_pct = gross_pct - bt.COMMISSION_RT_PCT
            slot_value[i] *= (1 + net_pct / 100.0)
            trades.append({'net_pct': net_pct, 'reason': reason})

    total_end = sum(slot_value)
    nets = [t['net_pct'] for t in trades]
    n_candidates_total = sum(len(bt.screen_at_date(cp, fundamentals_default, frames)) for cp in checkpoints)
    return {
        'return_pct': (total_end / (bt.SLOT_USD * bt.N_SLOTS) - 1) * 100,
        'n': len(trades),
        'winrate': sum(1 for x in nets if x > 0) / len(nets) * 100 if nets else 0,
        'candidates_seen': n_candidates_total,
    }


print(f"{'Период':>30} {'RSI<':>5} {'Доходность':>11} {'Сделок':>7} {'Winrate':>8} {'Кандидатов видено':>18}")
for label, frames_file, start, end in EPISODES:
    for rsi_limit in [45.0, 35.0]:
        r = run_episode(frames_file, start, end, rsi_limit)
        print(f"{label:>30} {rsi_limit:5.0f} {r['return_pct']:+10.2f}% {r['n']:7} {r['winrate']:7.1f}% {r['candidates_seen']:18}")
    print()
