"""Pair audition pipeline — systematically evaluate candidate pairs.

USAGE
=====
    python audit_pair.py DOGEUSDT SUIUSDT DOTUSDT ATOMUSDT
    python audit_pair.py --threshold -0.10 DOGEUSDT
    python audit_pair.py --quick DOGEUSDT     # Stage 1 only

PIPELINE
========
Stage 1 (cheap filter, 14d, single TP_R=2.0, ~3 min/pair):
  - Drop if expectancy < pass_threshold (default -0.15R).
  - Drop if no trades (illiquid / wrong symbol).

Stage 2 (full sweep on survivors, 30d, TP_R ∈ {1.5, 2.0, 2.5}, ~30 min/pair):
  - Pick the TP_R with best expectancy.
  - Report recommended config for inclusion in bot.py.

OUTPUT
======
Console table + audit_<timestamp>.csv with full per-pair, per-TP_R results.
Each successful candidate gets a one-line recommendation suitable for
pasting into bot.py PAIR_TP_R / PAIR_MIN_SCORE.
"""
import argparse
import csv
import sys
from datetime import datetime, timezone

import backtest  # reuse fetch + simulation logic


# Parameters that match the live bot's filter regime.
ADX_MIN     = 20.0
MIN_SCORE   = 92
SL_ATR      = 2.5
FEE_PCT     = 0.06
LEVERAGE    = 7
CAPITAL     = 99.61   # ≈ current bot balance
RISK_PCT    = 0.005


def run_one(symbol, days, tp_r):
    """Single backtest run. Returns summary dict or None on failure."""
    trades = backtest.backtest_symbol(
        symbol, days, MIN_SCORE, SL_ATR, tp_r, FEE_PCT, LEVERAGE,
        adx_min=ADX_MIN, strategy='v2',
    )
    if not trades:
        return None
    return backtest.report(symbol, trades, CAPITAL, RISK_PCT)


def stage1(symbol, threshold):
    """14d quick check. Returns (passed: bool, summary | None)."""
    print(f'\n{"="*60}\n  STAGE 1 — {symbol} (14d, TP_R=2.0)\n{"="*60}')
    s = run_one(symbol, days=14, tp_r=2.0)
    if not s:
        return False, None
    passed = s['expectancy_r'] >= threshold
    verdict = 'PASS' if passed else 'FAIL'
    print(f'  → {verdict}: expectancy {s["expectancy_r"]:+.3f}R (threshold {threshold:+.2f}R)')
    return passed, s


def stage2(symbol):
    """30d sweep TP_R ∈ {1.5, 2.0, 2.5}. Returns list of summaries."""
    print(f'\n{"="*60}\n  STAGE 2 — {symbol} (30d sweep)\n{"="*60}')
    results = []
    for tp_r in (1.5, 2.0, 2.5):
        print(f'\n  -- TP_R={tp_r} --')
        s = run_one(symbol, days=30, tp_r=tp_r)
        if s:
            s['tp_r'] = tp_r
            results.append(s)
    return results


def pick_best(results):
    """Choose TP_R with highest expectancy (ties → lower TP_R = faster exit)."""
    if not results:
        return None
    return max(results, key=lambda r: (r['expectancy_r'], -r['tp_r']))


def write_csv(rows, path):
    if not rows:
        return
    keys = list(rows[0].keys())
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('symbols', nargs='+', help='candidate symbols (e.g. DOGEUSDT SUIUSDT)')
    p.add_argument('--threshold', type=float, default=-0.15,
                   help='Stage 1 pass threshold in R (default -0.15)')
    p.add_argument('--quick', action='store_true', help='Stage 1 only, skip sweep')
    args = p.parse_args()

    print(f'Audit config: threshold={args.threshold:+.2f}R  quick={args.quick}  '
          f'pairs={len(args.symbols)}')

    passed = []
    failed = []
    s1_rows = []

    for sym in args.symbols:
        try:
            ok, summary = stage1(sym, args.threshold)
        except Exception as e:
            print(f'  [ERROR] {sym}: {e}')
            failed.append((sym, 'error'))
            continue
        if summary is None:
            failed.append((sym, 'no-data'))
            continue
        summary['stage'] = 1
        summary['tp_r'] = 2.0
        s1_rows.append(summary)
        if ok:
            passed.append(sym)
        else:
            failed.append((sym, f'EV {summary["expectancy_r"]:+.3f}R'))

    s2_rows = []
    recommendations = []
    if not args.quick and passed:
        for sym in passed:
            results = stage2(sym)
            for r in results:
                r['stage'] = 2
                s2_rows.append(r)
            best = pick_best(results)
            if best:
                recommendations.append((sym, best))

    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    write_csv(s1_rows + s2_rows, f'audit_{ts}.csv')

    print('\n' + '=' * 60)
    print('  FINAL REPORT')
    print('=' * 60)

    if failed:
        print('\n  DROP:')
        for sym, reason in failed:
            print(f'    × {sym}  ({reason})')

    if recommendations:
        print('\n  RECOMMEND (Stage 3 — live shadow with raised MIN_SCORE):')
        for sym, best in recommendations:
            print(f'    ✓ {sym}  TP_R={best["tp_r"]}  '
                  f'expectancy={best["expectancy_r"]:+.3f}R  '
                  f'WR={best["win_rate"]:.1f}%  '
                  f'trades={best["trades"]}')
        print('\n  Paste into bot.py for shadowing:')
        for sym, best in recommendations:
            print(f"    PAIR_TP_R['{sym}'] = {best['tp_r']}")
            print(f"    PAIR_MIN_SCORE['{sym}'] = 95   # raised from 92 for live shadow")
    elif args.quick and passed:
        print('\n  STAGE 1 PASSED (run without --quick for full sweep):')
        for sym in passed:
            print(f'    ✓ {sym}')

    print(f'\n  Full results → audit_{ts}.csv')


if __name__ == '__main__':
    main()
