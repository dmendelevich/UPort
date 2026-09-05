"""
Проверка предсказательной силы ranking_value (depth_ranking из _score_revolver)
-- действительно ли более высокий ранг (глубже просадка от 20-дневного
максимума, нормированная на волатильность) предсказывает лучший форвардный
результат, или порядок кандидатов внутри прошедших экран произволен.
Claude/BACKLOG.md #167, продолжение по просьбе пользователя ("входные
параметры зафиксировали для простоты -- не нужно ли поправить").

Метод: на каждом чек-пойнте каждого уже собранного эпизода берём ВЕСЬ список
прошедших экран (не только топ-10, которые реально покупались -- иначе
искусственное ограничение диапазона исказит корреляцию), считаем безусловную
10-дневную доходность "держать не глядя" (та же метрика hold10_pct, что и в
MAE/MFE-анализе, НЕ через SL/TP -- чтобы не вносить искажение от цензурирования
по правилу выхода). 2023 год сознательно исключён -- уже известно, что тест
испорчен нехваткой кандидатов (см. README).
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import numpy as np
import pandas as pd
from scipy import stats
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'
with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}

EPISODES = [
    ('Эп.№1 (апр-сен 2026, бычий)', 'signal_frames.pkl', date(2026, 4, 1), date(2026, 9, 5)),
    ('2022 (медленный медвежий)', 'signal_frames_2022.pkl', date(2022, 1, 3), date(2022, 3, 1)),
    ('Фев-апр 2025 (резкий медвежий)', 'signal_frames_2025_feb_apr.pkl', date(2025, 2, 1), date(2025, 4, 21)),
    ('Апр25-фев26 (бычий)', 'signal_frames_2025_2026_bull.pkl', date(2025, 4, 21), date(2026, 2, 20)),
]

HOLD_DAYS = 10
rows = []

for label, cache_file, start, end in EPISODES:
    with open(CACHE_DIR / cache_file, 'rb') as f:
        frames = pickle.load(f)
    checkpoints = []
    d = start
    while d <= end:
        checkpoints.append(d)
        d += timedelta(days=10)

    for cp in checkpoints:
        candidates = bt.screen_at_date(cp, fundamentals, frames)  # ВЕСЬ список, не топ-10
        for sym, rank, price in candidates:
            df = frames[sym]
            start_ts, end_ts = pd.Timestamp(cp), pd.Timestamp(cp + timedelta(days=HOLD_DAYS))
            window = df[(df.index > start_ts) & (df.index <= end_ts)]
            if len(window) == 0:
                continue
            hold10_pct = (float(window['close'].iloc[-1]) / price - 1) * 100
            mae_pct = (float(window['low'].min()) / price - 1) * 100
            rows.append({'episode': label, 'symbol': sym, 'entry_date': str(cp),
                         'ranking_value': rank, 'hold10_pct': hold10_pct, 'mae_pct': mae_pct})

df = pd.DataFrame(rows)
print(f"Всего наблюдений (кандидат x чек-пойнт), пул всех 4 эпизодов: {len(df)}\n")

print("=== По эпизодам ===")
for ep, g in df.groupby('episode'):
    print(f"  {ep}: n={len(g)}")

rho, pval = stats.spearmanr(df['ranking_value'], df['hold10_pct'])
print(f"\n=== Spearman-корреляция ranking_value vs 10-дневная доходность (весь пул) ===")
print(f"rho={rho:.4f}  p-value={pval:.4f}  {'ЗНАЧИМО' if pval < 0.05 else 'НЕ значимо'} на уровне 5%")

print(f"\n=== Квинтили по ranking_value (1=низший ранг, 5=высший) ===")
df['quintile'] = pd.qcut(df['ranking_value'], 5, labels=False, duplicates='drop') + 1
summary = df.groupby('quintile')['hold10_pct'].agg(['count', 'mean', 'median', 'std'])
print(summary.round(2))

print(f"\n=== То же самое, но ПО КАЖДОМУ ЭПИЗОДУ ОТДЕЛЬНО (регулярность внутри режима?) ===")
for ep, g in df.groupby('episode'):
    if len(g) < 20:
        print(f"  {ep}: n={len(g)} -- слишком мало для квинтилей, пропуск")
        continue
    rho_ep, pval_ep = stats.spearmanr(g['ranking_value'], g['hold10_pct'])
    g = g.copy()
    g['q'] = pd.qcut(g['ranking_value'], 5, labels=False, duplicates='drop') + 1
    means = g.groupby('q')['hold10_pct'].mean().round(2).to_dict()
    print(f"  {ep}: n={len(g)}  rho={rho_ep:+.3f} (p={pval_ep:.3f})  квинтили(1->5)={means}")

df.to_csv(CACHE_DIR.parent / 'ranking_predictive_power_detail.csv', index=False)
print(f"\nПодробности сохранены в ranking_predictive_power_detail.csv")
