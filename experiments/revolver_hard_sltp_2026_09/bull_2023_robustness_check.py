"""
Симметричная проверка на надёжность для БЫЧЬЕЙ стороны (2026-09-05,
Claude/BACKLOG.md #167) -- та же логика, что и bear_2022_robustness_check.py,
только для растущего рынка: подтвердится ли "лучший бычий" набор (SL=-10%/
TP=+10%, 10-дневный цикл) на ДРУГОМ реальном растущем периоде, не только на
апреле-сентябре 2026.

Период: 03.01-01.06.2023 -- реальное восстановление рынка после медвежьего 2022
года (VTI ~+9.25% за этот период, того же порядка величины, что и в оригинальном
тесте, хотя и не идентично) -- по духу похоже на наш ориг. тест (V-образный
отскок + продолженный рост), не случайный выбор года.
"""
import sys, json, warnings, pickle
warnings.filterwarnings('ignore')
sys.path.insert(0, '/root/UPort')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from datetime import date, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
import settings
import backtest as bt

CACHE_DIR = __import__('pathlib').Path(__file__).resolve().parent / '_cache'

with open(CACHE_DIR / 'sp500_fundamentals.json') as f:
    fundamentals = {r['symbol']: r for r in json.load(f)}
symbols = list(fundamentals.keys())

CACHE = str(CACHE_DIR / 'signal_frames_2023.pkl')
try:
    with open(CACHE, 'rb') as f:
        frames = pickle.load(f)
    print(f"Кэш найден: {len(frames)} тикеров")
except FileNotFoundError:
    print("Скачиваю историю цен за 2022-2023...")
    raw = yf.download(symbols, start='2022-10-01', end='2023-06-15', interval='1d',
                       group_by='ticker', threads=True, progress=False, auto_adjust=False)
    frames = {}
    for sym in symbols:
        try:
            df = raw[sym].dropna(subset=['Close'])
        except Exception:
            continue
        if df is None or len(df) < 60:
            continue
        close, vol = df['Close'], df['Volume']
        ema20 = close.ewm(span=20, adjust=False).mean()
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta.where(delta < 0, 0))
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 0.00001)
        rsi = 100 - (100 / (1 + rs))
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        vol_pct = close.pct_change().rolling(settings.DAILY_VOLATILITY_WINDOW_DAYS).std() * 100
        above = (close > ema20)
        grp = (above != above.shift()).cumsum()
        streak_raw = above.groupby(grp).cumcount() + 1
        streak = np.where(above, streak_raw, -streak_raw)
        vol_ratio_20d = vol / vol.rolling(20).mean()
        high_20d = close.rolling(20).max()
        price_to_20d_high_pct = (close / high_20d - 1) * 100
        turnover_usd = vol * close
        frames[sym] = pd.DataFrame({
            'close': close, 'rsi': rsi, 'macd': macd, 'vol_pct': vol_pct,
            'streak': streak, 'vol_ratio_20d': vol_ratio_20d,
            'price_to_20d_high_pct': price_to_20d_high_pct, 'turnover_usd': turnover_usd,
            'high': df['High'], 'low': df['Low'],
        })
    with open(CACHE, 'wb') as f:
        pickle.dump(frames, f)
    print(f"Готово, {len(frames)} тикеров")

bt.RESCAN_DAYS = 10
bt.TIMEOUT_DAYS = 10

START_DATE = date(2023, 1, 3)
END_DATE = date(2023, 6, 1)
checkpoints = []
d = START_DATE
while d <= END_DATE:
    checkpoints.append(d)
    d += timedelta(days=bt.RESCAN_DAYS)

screens = {cp: bt.screen_at_date(cp, fundamentals, frames)[:bt.N_SLOTS] for cp in checkpoints}
print(f"Кандидатов по чек-пойнтам: {[len(bt.screen_at_date(cp, fundamentals, frames)) for cp in checkpoints]}")

def run_combo(sl_pct, tp_pct):
    slot_value = [bt.SLOT_USD] * bt.N_SLOTS
    trades = []
    for cp in checkpoints:
        chosen = screens[cp]
        for i in range(bt.N_SLOTS):
            if i >= len(chosen): continue
            sym, rank, price = chosen[i]
            bt.SL_PCT, bt.TP_PCT = sl_pct, tp_pct
            exit_price, exit_date, reason = bt.simulate_slot(sym, cp, price, frames)
            gross_pct = (exit_price - price) / price * 100.0
            net_pct = gross_pct - bt.COMMISSION_RT_PCT
            slot_value[i] *= (1 + net_pct / 100.0)
            trades.append({'net_pct': net_pct, 'reason': reason})
    total_end = sum(slot_value)
    reasons = {}
    for t in trades: reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    nets = [t['net_pct'] for t in trades]
    return {'return_pct': (total_end/(bt.SLOT_USD*bt.N_SLOTS)-1)*100, 'n': len(trades), 'reasons': reasons,
            'winrate': sum(1 for n in nets if n>0)/len(nets)*100 if nets else 0}

print(f"\n=== 'Лучший бычий' набор (SL=-10/TP=+10) на ДРУГОМ бычьем периоде (2023) ===")
r = run_combo(10.0, 10.0)
print(f"Доходность: {r['return_pct']:+.2f}%  n={r['n']}  winrate={r['winrate']:.1f}%  {r['reasons']}")

print(f"\n=== Для контекста -- та же сетка SL/TP, что гоняли на 2026 ===")
print(f"{'SL%':>5} {'TP%':>5} {'Доходность':>11} {'Winrate':>8} {'SL':>4} {'TP':>4} {'Timeout':>8}")
best = None
for sl in [6.0, 7.0, 10.0]:
    for tp in [6.0, 10.0, 15.0, 20.0]:
        r = run_combo(sl, tp)
        if best is None or r['return_pct'] > best[2]['return_pct']:
            best = (sl, tp, r)
        print(f"{sl:5.1f} {tp:5.1f} {r['return_pct']:+10.2f}% {r['winrate']:7.1f}% {r['reasons'].get('SL',0):4} {r['reasons'].get('TP',0):4} {r['reasons'].get('TIMEOUT',0):8}")

print(f"\nЛучший на бычьем 2023: SL=-{best[0]}% TP=+{best[1]}% -> {best[2]['return_pct']:+.2f}%")

vti = yf.Ticker('VTI').history(start='2023-01-03', end='2023-06-02')
vti_ret = (vti['Close'].iloc[-1]/vti['Close'].iloc[0]-1)*100
print(f"VTI buy-and-hold за тот же период: {vti_ret:+.2f}%")
