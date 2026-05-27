"""One-shot runner: top 11-20 audit, 10 symbols x TP_R=2.0 x 30d.

Append results to existing audit_top11_20_20260527.csv (XMR/BCH/TON already
written by earlier agent). Uses ThreadPoolExecutor with 3 workers — balances
Bitget rate-limit risk vs wall-clock time (~12-15 min vs ~25 min serial).

Read-only on prod code; bot.py / db.py untouched.
"""
import csv
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '.')
import backtest


SYMBOLS = ['LINKUSDT', 'CCUSDT', 'XLMUSDT', 'MUSDT', 'LTCUSDT',
           'SUIUSDT', 'ICPUSDT', 'AVAXUSDT', 'BGBUSDT', 'UNIUSDT']
CSV_PATH = 'audit_top11_20_20260527.csv'

# Same config as audit_top10 + live bot
MIN_SCORE = 92
SL_ATR = 2.5
TP_R = 2.0
FEE_PCT = 0.06
LEVERAGE = 7
ADX_MIN = 20.0
DAYS = 30
CAPITAL = 99.61
RISK_PCT = 0.005


def run_one(symbol):
    t0 = time.time()
    try:
        trades = backtest.backtest_symbol(
            symbol, DAYS, MIN_SCORE, SL_ATR, TP_R, FEE_PCT, LEVERAGE,
            adx_min=ADX_MIN, strategy='v2',
        )
        if trades is None:
            return {'symbol': symbol, 'status': 'NO_DATA', 'error': 'fetch returned None',
                    'elapsed_sec': time.time() - t0}
        if not trades:
            return {'symbol': symbol, 'status': 'NO_TRADES', 'trades': 0,
                    'elapsed_sec': time.time() - t0}
        summary = backtest.report(symbol, trades, CAPITAL, RISK_PCT) or {}
        long_count = sum(1 for t in trades if t['side'] == 'long')
        short_count = sum(1 for t in trades if t['side'] == 'short')
        wins = [t for t in trades if t['r_net'] > 0]
        losses = [t for t in trades if t['r_net'] <= 0]
        avg_score_w = sum(t['score'] for t in wins) / len(wins) if wins else 0.0
        avg_score_l = sum(t['score'] for t in losses) / len(losses) if losses else 0.0
        total = max(long_count + short_count, 1)
        ls_balance = f'{long_count*100//total}%L/{short_count*100//total}%S'
        notes = ''
        if long_count == 0:
            notes = '0 longs -- short-only behavior'
        elif short_count == 0:
            notes = '0 shorts -- long-only behavior'
        return {
            'symbol': symbol,
            'trades': summary.get('trades', len(trades)),
            'win_rate': round(summary.get('win_rate', 0), 1),
            'expectancy_R': round(summary.get('expectancy_r', 0), 3),
            'profit_factor': round(summary.get('profit_factor', 0), 2),
            'max_dd_pct': round(summary.get('max_dd_pct', 0), 1),
            'long_count': long_count,
            'short_count': short_count,
            'ls_balance': ls_balance,
            'avg_score_winners': round(avg_score_w, 2),
            'avg_score_losers': round(avg_score_l, 2),
            'status': 'OK',
            'notes': notes,
            'elapsed_sec': time.time() - t0,
        }
    except Exception as e:
        traceback.print_exc()
        return {'symbol': symbol, 'status': 'ERROR', 'error': str(e),
                'elapsed_sec': time.time() - t0}


def main():
    print(f'[runner] starting {len(SYMBOLS)} backtests, max_workers=3', flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(run_one, sym): sym for sym in SYMBOLS}
        for f in as_completed(futures):
            sym = futures[f]
            r = f.result()
            print(f'[done] {sym} status={r.get("status")} '
                  f'exp={r.get("expectancy_R")}R trades={r.get("trades")} '
                  f'elapsed={r.get("elapsed_sec", 0):.0f}s', flush=True)
            results.append(r)

    # Append to existing CSV (XMR/BCH/TON already there)
    cols = ['symbol', 'trades', 'win_rate', 'expectancy_R', 'profit_factor',
            'max_dd_pct', 'long_count', 'short_count', 'ls_balance',
            'avg_score_winners', 'avg_score_losers', 'status', 'notes']
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        for r in results:
            w.writerow([r.get(c, '') for c in cols])

    print(f'[runner] wrote {len(results)} rows to {CSV_PATH}', flush=True)


if __name__ == '__main__':
    main()
