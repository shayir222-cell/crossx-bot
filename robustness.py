"""Robustness pre-deploy gate for a candidate trading pair.

Runs the 4 robustness gates defined in the pair selection methodology
(tasks.md / project_pair_selection_methodology.md):

    Q1 -- Edge stability across two non-overlapping 30d windows
         (today-30..today vs today-90..today-60). Both must show
         expectancy >= 0 in R (using r_net = fees-included).

    Q2 -- Score-bucket monotonicity. Spearman rho between score
         bucket midpoint and bucket-mean r_net. Pass if rho >= 0.

    Q3 -- Long/short balance: min(L,S) / max(L,S) >= 0.20 on window A.

    Q4 -- Correlation veto: 30d 1H log-return Pearson correlation with
         the reference symbol (default TONUSDT, current production pair).
         PASS  if < 0.60
         GRAY  if 0.60 <= rho < 0.70   (recommend 60d tiebreaker)
         FAIL  if >= 0.70

Each check emits PASS / FAIL / INCONCLUSIVE / GRAY plus diagnostics.
Overall verdict aggregates: FAIL if any FAIL; else GRAY if any GRAY;
else INCONCLUSIVE if any INCONCLUSIVE; else PASS.

Backtest is invoked as a subprocess (clean isolation; ~5 min per window).
Q1 uses a single --days 90 run, post-filtered by ts into window A and B,
because backtest.py has no --start-date flag.

Usage:
    python robustness.py --symbol ZECUSDT
    python robustness.py --symbol HYPEUSDT --reference-symbol TONUSDT
    python robustness.py --symbol BNBUSDT     # sanity: expected FAIL

Output convention:
    JSON   -> stdout
    Human  -> stderr
Exit 0 even on failed gates; check JSON 'overall' field.
"""
import argparse
import csv
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone


# Production strategy constants -- these MUST match bot.py to test the right
# strategy. We attempt to import live values; if that fails we fall back to
# the audit_pair.py defaults and emit a warning so the operator sees the drift.
_CONFIG_SOURCE = 'defaults'
_CONFIG_IMPORT_ERROR = None
# Use direct attribute access (not getattr with default) so a rename in bot.py
# triggers AttributeError -> fall through to defaults with a visible reason.
# Silent default-fallback would mean the gate tests the wrong strategy.
try:
    import bot as _bot  # type: ignore
    DEFAULT_TP_R = float(_bot.TP1_R)
    DEFAULT_MIN_SCORE = int(_bot.MIN_SCORE)
    DEFAULT_SL_ATR = float(_bot.SL_ATR_MULT)
    DEFAULT_ADX_MIN = float(_bot.ADX_MIN)
    DEFAULT_FEE_PCT = float(_bot.FEE_PCT)
    DEFAULT_LEVERAGE = int(_bot.LEVERAGE)
    _CONFIG_SOURCE = 'bot.py'
except (ImportError, AttributeError, ValueError, TypeError) as _e:
    _CONFIG_IMPORT_ERROR = f'{type(_e).__name__}: {_e}'
    DEFAULT_TP_R = 2.0
    DEFAULT_MIN_SCORE = 92
    DEFAULT_SL_ATR = 2.5
    DEFAULT_ADX_MIN = 20.0
    DEFAULT_FEE_PCT = 0.06
    DEFAULT_LEVERAGE = 7
except Exception as _e:
    _CONFIG_IMPORT_ERROR = f'{type(_e).__name__}: {_e}'
    DEFAULT_TP_R = 2.0
    DEFAULT_MIN_SCORE = 92
    DEFAULT_SL_ATR = 2.5
    DEFAULT_ADX_MIN = 20.0
    DEFAULT_FEE_PCT = 0.06
    DEFAULT_LEVERAGE = 7

# Test thresholds
Q1_MIN_TRADES_PER_WINDOW = 20      # below this, INCONCLUSIVE
DEFAULT_Q1_THRESHOLD_R = 0.0       # exp >= 0 in BOTH windows; pass --q1-threshold 0.05 to align with live decision gate
Q2_MIN_NON_EMPTY_BUCKETS = 3
Q2_FAIL_RHO = 0.0                  # rho < 0 = anti-predictive => FAIL
Q2_PASS_RHO = 0.30                 # rho >= 0.30 => PASS; [0, 0.30) => GRAY (weak signal)
Q3_MIN_TOTAL_TRADES = 20
Q3_BALANCE_THRESHOLD = 0.20
Q4_GRAY_LO = 0.60                  # tighter than spec's 0.70 -- defensible, document below
Q4_FAIL_HI = 0.70

# Score buckets: [92, 94), [94, 96), [96, 98), [98, 101)
SCORE_BUCKETS = [(92, 94), (94, 96), (96, 98), (98, 101)]
SCORE_MIDPOINTS = [(lo + hi - 1) / 2.0 for lo, hi in SCORE_BUCKETS]

SUBPROCESS_TIMEOUT_SEC = 900
WINDOW_A_DAYS = 30
WINDOW_B_LOOKBACK_DAYS = 90
WINDOW_B_END_DAYS = 60  # window B ends 60d ago


def _now_ms():
    return int(time.time() * 1000)


def _ms_per_day():
    return 24 * 3600 * 1000


def _run_backtest(symbol, days, bt_script, python_exe):
    """Invoke backtest.py via subprocess, return path to CSV log.

    Returns (csv_path, stdout_text) or (None, error_text) on failure.
    """
    cmd = [
        python_exe, bt_script,
        '--symbol', symbol,
        '--days', str(days),
        '--tp_r', str(DEFAULT_TP_R),
        '--min_score', str(DEFAULT_MIN_SCORE),
        '--sl_atr', str(DEFAULT_SL_ATR),
        '--adx_min', str(DEFAULT_ADX_MIN),
        '--fee_pct', str(DEFAULT_FEE_PCT),
        '--leverage', str(DEFAULT_LEVERAGE),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           timeout=SUBPROCESS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None, f'backtest timed out after {SUBPROCESS_TIMEOUT_SEC}s'
    except OSError as e:
        return None, f'backtest could not start: {e}'

    out = (r.stdout or '') + '\n' + (r.stderr or '')
    if r.returncode != 0:
        return None, f'backtest exited {r.returncode}: {out[-500:]}'
    m = re.search(r'Trade log\s*->\s*(\S+\.csv)', out)
    if not m:
        return None, f'no "Trade log -> ..." line in output: {out[-500:]}'
    csv_path = m.group(1).strip()
    if not os.path.exists(csv_path):
        return None, f'csv path {csv_path} parsed but not found on disk'
    return csv_path, out


def _load_trades(csv_path, symbol):
    """Read backtest CSV, filter to symbol, return (list of dicts, n_dropped)."""
    rows = []
    n_dropped = 0
    with open(csv_path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r.get('symbol') != symbol:
                continue
            try:
                r['ts'] = int(r['ts'])
                r['r_net'] = float(r['r_net'])
                r['r_value'] = float(r['r_value'])
                r['score'] = int(r['score'])
                r['atr_pct'] = float(r['atr_pct'])
                side = r.get('side')
                if side not in ('long', 'short'):
                    n_dropped += 1
                    continue
                r['side'] = side
            except (ValueError, KeyError, TypeError):
                n_dropped += 1
                continue
            rows.append(r)
    return rows, n_dropped


def _partition_windows(trades, now_ms):
    """Split trades into window A and window B by ts.

    A: [now - 30d, now]
    B: [now - 90d, now - 60d]
    Trades outside both windows (the 30d-60d gap) are discarded.
    """
    a_lo = now_ms - WINDOW_A_DAYS * _ms_per_day()
    b_lo = now_ms - WINDOW_B_LOOKBACK_DAYS * _ms_per_day()
    b_hi = now_ms - WINDOW_B_END_DAYS * _ms_per_day()
    a = [t for t in trades if t['ts'] >= a_lo]
    b = [t for t in trades if b_lo <= t['ts'] < b_hi]
    return a, b


def _expectancy(trades):
    if not trades:
        return None
    return sum(t['r_net'] for t in trades) / len(trades)


def _q1(trades_all_window, now_ms, q1_threshold):
    """Q1 -- edge stability across two non-overlapping 30d windows.

    Both windows must have expectancy >= q1_threshold (default 0).
    Pass --q1-threshold 0.05 to align with the live decision gate.
    """
    window_a, window_b = _partition_windows(trades_all_window, now_ms)
    exp_a = _expectancy(window_a)
    exp_b = _expectancy(window_b)
    n_a = len(window_a)
    n_b = len(window_b)

    diag = {
        'n_window_a': n_a, 'n_window_b': n_b,
        'expectancy_a': exp_a, 'expectancy_b': exp_b,
        'q1_threshold': q1_threshold,
        'window_a_days': '[today-30, today]',
        'window_b_days': '[today-90, today-60]',
    }

    if n_a < Q1_MIN_TRADES_PER_WINDOW and n_b < Q1_MIN_TRADES_PER_WINDOW:
        return {'status': 'INCONCLUSIVE',
                'reason': f'both windows have <{Q1_MIN_TRADES_PER_WINDOW} trades (a={n_a}, b={n_b})',
                **diag}, window_a
    if n_a < Q1_MIN_TRADES_PER_WINDOW:
        return {'status': 'INCONCLUSIVE',
                'reason': f'window A has <{Q1_MIN_TRADES_PER_WINDOW} trades (n={n_a})',
                **diag}, window_a
    if n_b < Q1_MIN_TRADES_PER_WINDOW:
        return {'status': 'INCONCLUSIVE',
                'reason': f'window B has <{Q1_MIN_TRADES_PER_WINDOW} trades (n={n_b}); '
                          f'asset may be too new for 90d history',
                **diag}, window_a

    if exp_a >= q1_threshold and exp_b >= q1_threshold:
        return {'status': 'PASS',
                'reason': f'expectancy A={exp_a:+.3f}R, B={exp_b:+.3f}R '
                          f'(threshold {q1_threshold:+.3f}R)',
                **diag}, window_a
    fails = []
    if exp_a < q1_threshold:
        fails.append(f'A={exp_a:+.3f}R')
    if exp_b < q1_threshold:
        fails.append(f'B={exp_b:+.3f}R')
    return {'status': 'FAIL',
            'reason': f'expectancy < {q1_threshold:+.3f}R in ' + ', '.join(fails),
            **diag}, window_a


def _q2(window_a_trades):
    """Q2 -- score-bucket monotonicity (Spearman rho >= 0)."""
    if not window_a_trades:
        return {'status': 'INCONCLUSIVE',
                'reason': 'no trades in window A'}

    bucket_means = []
    bucket_counts = []
    bucket_labels = []
    for lo, hi in SCORE_BUCKETS:
        bucket_trades = [t for t in window_a_trades if lo <= t['score'] < hi]
        if bucket_trades:
            bucket_means.append(
                sum(t['r_net'] for t in bucket_trades) / len(bucket_trades))
            bucket_counts.append(len(bucket_trades))
        else:
            bucket_means.append(None)
            bucket_counts.append(0)
        bucket_labels.append(f'[{lo},{hi})')

    non_empty_idx = [i for i, m in enumerate(bucket_means) if m is not None]
    if len(non_empty_idx) < Q2_MIN_NON_EMPTY_BUCKETS:
        return {
            'status': 'INCONCLUSIVE',
            'reason': f'only {len(non_empty_idx)} non-empty score buckets, '
                      f'need >={Q2_MIN_NON_EMPTY_BUCKETS}',
            'buckets': [
                {'label': bucket_labels[i], 'n': bucket_counts[i],
                 'mean_r_net': bucket_means[i]}
                for i in range(len(SCORE_BUCKETS))],
        }

    midpoints = [SCORE_MIDPOINTS[i] for i in non_empty_idx]
    means = [bucket_means[i] for i in non_empty_idx]
    try:
        rho = statistics.correlation(midpoints, means, method='ranked')
    except (statistics.StatisticsError, AttributeError) as e:
        return {'status': 'INCONCLUSIVE',
                'reason': f'spearman computation failed: {e}'}

    if rho < Q2_FAIL_RHO:
        status = 'FAIL'
        narrative = 'anti-predictive'
    elif rho < Q2_PASS_RHO:
        status = 'GRAY'
        narrative = 'weak / inconclusive signal'
    else:
        status = 'PASS'
        narrative = 'monotonic+'
    caveat = None
    if len(non_empty_idx) == Q2_MIN_NON_EMPTY_BUCKETS:
        caveat = f'low statistical power (only {Q2_MIN_NON_EMPTY_BUCKETS} buckets)'
    return {
        'status': status,
        'spearman_rho': rho,
        'reason': f'spearman rho={rho:+.3f} ({narrative}; '
                  f'FAIL<{Q2_FAIL_RHO}, GRAY [{Q2_FAIL_RHO},{Q2_PASS_RHO}), PASS>={Q2_PASS_RHO})',
        'caveat': caveat,
        'buckets': [
            {'label': bucket_labels[i], 'n': bucket_counts[i],
             'mean_r_net': bucket_means[i]}
            for i in range(len(SCORE_BUCKETS))],
    }


def _q3(window_a_trades):
    """Q3 -- long/short balance >= 0.20."""
    n = len(window_a_trades)
    if n < Q3_MIN_TOTAL_TRADES:
        return {'status': 'INCONCLUSIVE',
                'reason': f'only {n} trades in window A, need >={Q3_MIN_TOTAL_TRADES}',
                'n_long': sum(1 for t in window_a_trades if t['side'] == 'long'),
                'n_short': sum(1 for t in window_a_trades if t['side'] == 'short')}

    n_long = sum(1 for t in window_a_trades if t['side'] == 'long')
    n_short = sum(1 for t in window_a_trades if t['side'] == 'short')
    if max(n_long, n_short) == 0:
        return {'status': 'FAIL', 'reason': 'no recognizable side data',
                'n_long': n_long, 'n_short': n_short}
    ratio = min(n_long, n_short) / max(n_long, n_short)
    status = 'PASS' if ratio >= Q3_BALANCE_THRESHOLD else 'FAIL'
    return {
        'status': status,
        'n_long': n_long, 'n_short': n_short, 'balance_ratio': ratio,
        'reason': f'L={n_long} S={n_short} ratio={ratio:.2f}',
    }


def _aligned_log_returns(symbol_a, symbol_b, days, fetch_fn):
    """Fetch 1H closes for both symbols, inner-join on ts, return log-return pairs.

    Delegates to correlation_utils for the actual math so robustness.py and
    correlation_matrix.py share one implementation. Returns (ra, rb, diag)
    on success, or (None, None, error_string) on failure.
    """
    from correlation_utils import fetch_closes_map, pair_log_returns_from_maps
    map_a, err_a = fetch_closes_map(symbol_a, '1H', days, fetch_fn)
    if err_a:
        return None, None, f'fetch {symbol_a}: {err_a}'
    map_b, err_b = fetch_closes_map(symbol_b, '1H', days, fetch_fn)
    if err_b:
        return None, None, f'fetch {symbol_b}: {err_b}'
    ra, rb, diag = pair_log_returns_from_maps(map_a, map_b)
    if diag['n_common_candles'] < 50:
        return None, None, f'too few aligned candles: {diag["n_common_candles"]}'
    return ra, rb, diag


def _q4_single(symbol, reference_symbol, days, fetch_fn):
    """Compute correlation with one reference symbol. Returns (rho, n, diag, err)."""
    if symbol == reference_symbol:
        return None, 0, None, 'symbol == reference_symbol'
    ra, rb, diag = _aligned_log_returns(symbol, reference_symbol, days, fetch_fn)
    if isinstance(diag, str):  # error string from fetch failure
        return None, 0, None, diag
    if not ra or len(ra) < 50:
        return None, len(ra) if ra else 0, diag, f'too few aligned returns: {len(ra) if ra else 0} (gaps dropped: {diag.get("n_gap_dropped") if diag else "?"})'
    try:
        rho = statistics.correlation(ra, rb)
    except statistics.StatisticsError as e:
        return None, len(ra), diag, f'pearson failed: {e}'
    return rho, len(ra), diag, None


def _q4(symbol, reference_symbols, days, fetch_fn):
    """Q4 -- correlation veto: Pearson on log-returns vs each reference symbol.

    reference_symbols: list of symbols to check against. Takes the MAX
    correlation across them. PASS if max < 0.60, GRAY if [0.60, 0.70), FAIL >= 0.70.
    """
    if not fetch_fn:
        return {'status': 'INCONCLUSIVE',
                'reason': 'no fetch function available (backtest import failed)',
                'reference_symbols': reference_symbols}
    if not reference_symbols:
        return {'status': 'INCONCLUSIVE',
                'reason': 'no reference symbols provided',
                'reference_symbols': reference_symbols}

    per_ref = []
    errors = []
    for ref in reference_symbols:
        rho, n_aligned, diag, err = _q4_single(symbol, ref, days, fetch_fn)
        if err:
            errors.append(f'{ref}: {err}')
            continue
        per_ref.append({'reference_symbol': ref, 'pearson_rho': rho,
                        'n_aligned_returns': n_aligned})

    if not per_ref:
        return {'status': 'INCONCLUSIVE',
                'reason': '; '.join(errors) if errors else 'no usable references',
                'reference_symbols': reference_symbols,
                'per_reference': []}

    max_rho_entry = max(per_ref, key=lambda x: x['pearson_rho'])
    max_rho = max_rho_entry['pearson_rho']
    if max_rho < Q4_GRAY_LO:
        status = 'PASS'
    elif max_rho < Q4_FAIL_HI:
        status = 'GRAY'
    else:
        status = 'FAIL'
    reason = (f'max corr={max_rho:+.3f} vs {max_rho_entry["reference_symbol"]} '
              f'(PASS<{Q4_GRAY_LO}, GRAY [{Q4_GRAY_LO},{Q4_FAIL_HI}), FAIL>={Q4_FAIL_HI})')
    if errors:
        reason += f'; errors: {"; ".join(errors)}'
    return {
        'status': status,
        'max_pearson_rho': max_rho,
        'reference_symbols': reference_symbols,
        'per_reference': per_ref,
        'reason': reason,
    }


def _aggregate_overall(q1, q2, q3, q4):
    """Aggregate per-Q statuses into one overall verdict."""
    statuses = [q1['status'], q2['status'], q3['status'], q4['status']]
    if any(s == 'FAIL' for s in statuses):
        return 'FAIL'
    if any(s == 'GRAY' for s in statuses):
        return 'GRAY'
    if any(s == 'INCONCLUSIVE' for s in statuses):
        return 'INCONCLUSIVE'
    return 'PASS'


def run_checks(symbol, reference_symbols, bt_script, python_exe,
               fetch_fn, q1_threshold=DEFAULT_Q1_THRESHOLD_R,
               now_ms=None, csv_override=None):
    """Top-level orchestration. Returns full result dict.

    reference_symbols: list of reference symbols (production pairs) for Q4.
    csv_override: skip the backtest subprocess and read trades from this CSV.
    """
    now_ms = now_ms if now_ms is not None else _now_ms()
    out = {
        'symbol': symbol,
        'reference_symbols': reference_symbols,
        'config_source': _CONFIG_SOURCE,
        'config': {
            'tp_r': DEFAULT_TP_R, 'min_score': DEFAULT_MIN_SCORE,
            'sl_atr': DEFAULT_SL_ATR, 'adx_min': DEFAULT_ADX_MIN,
            'fee_pct': DEFAULT_FEE_PCT, 'leverage': DEFAULT_LEVERAGE,
        },
        'q1_threshold': q1_threshold,
        'now_utc': datetime.fromtimestamp(now_ms / 1000.0,
                                          tz=timezone.utc).isoformat(),
        'q1': {'status': 'INCONCLUSIVE'},
        'q2': {'status': 'INCONCLUSIVE'},
        'q3': {'status': 'INCONCLUSIVE'},
        'q4': {'status': 'INCONCLUSIVE'},
        'overall': 'INCONCLUSIVE',
        'notes': [],
    }
    if _CONFIG_SOURCE == 'defaults':
        reason = _CONFIG_IMPORT_ERROR or 'unknown'
        out['notes'].append(
            f'WARNING: bot.py config not imported ({reason}); '
            f'using hardcoded defaults -- verify they match live')

    # Run the 90d backtest (one subprocess, post-filtered into 2 windows)
    if csv_override:
        csv_path = csv_override
        out['notes'].append(f'csv_override={csv_path}')
    else:
        csv_path, info = _run_backtest(
            symbol, WINDOW_B_LOOKBACK_DAYS, bt_script, python_exe)
        if csv_path is None:
            out['q1'] = {'status': 'INCONCLUSIVE',
                         'reason': 'backtest invocation failed',
                         'detail': info}
            out['q2'] = {'status': 'INCONCLUSIVE',
                         'reason': 'no backtest output'}
            out['q3'] = {'status': 'INCONCLUSIVE',
                         'reason': 'no backtest output'}
            out['q4'] = _q4(symbol, reference_symbols, 30, fetch_fn)
            out['overall'] = _aggregate_overall(out['q1'], out['q2'], out['q3'], out['q4'])
            return out
        out['notes'].append(f'backtest_csv={csv_path}')

    trades, n_dropped = _load_trades(csv_path, symbol)
    out['notes'].append(f'n_trades_total={len(trades)}, n_dropped_parse={n_dropped}')
    if n_dropped > 0:
        out['notes'].append(f'WARNING: {n_dropped} CSV rows dropped for parse errors')

    if not trades and csv_override:
        out['notes'].append(f'WARNING: csv_override has no rows matching symbol={symbol}')

    q1_result, window_a = _q1(trades, now_ms, q1_threshold)
    out['q1'] = q1_result
    out['q2'] = _q2(window_a)
    out['q3'] = _q3(window_a)
    out['q4'] = _q4(symbol, reference_symbols, 30, fetch_fn)
    out['overall'] = _aggregate_overall(out['q1'], out['q2'], out['q3'], out['q4'])
    return out


def _format_human(result):
    def fmt(v, n=3, signed=True):
        if v is None:
            return 'n/a'
        if isinstance(v, float):
            return f'{v:+.{n}f}' if signed else f'{v:.{n}f}'
        return str(v)

    lines = []
    refs = result.get('reference_symbols') or []
    lines.append(f"=== Robustness check: {result['symbol']} (refs={','.join(refs)}) ===")
    lines.append(f"now: {result['now_utc']}  config_source: {result.get('config_source')}")
    lines.append('')

    q1 = result['q1']
    lines.append(f"Q1 (edge stability)    : {q1['status']:<14} {q1.get('reason', '')}")
    if 'expectancy_a' in q1:
        lines.append(f"   window A [today-30, today] : exp={fmt(q1.get('expectancy_a'))}R  n={q1.get('n_window_a')}")
        lines.append(f"   window B [today-90, today-60]: exp={fmt(q1.get('expectancy_b'))}R  n={q1.get('n_window_b')}")

    q2 = result['q2']
    lines.append(f"Q2 (score monotonicity): {q2['status']:<14} {q2.get('reason', '')}")
    if 'buckets' in q2:
        for b in q2['buckets']:
            lines.append(f"   score {b['label']:<8}: n={b['n']:<3}  mean_r_net={fmt(b.get('mean_r_net'))}")

    q3 = result['q3']
    lines.append(f"Q3 (L/S balance)       : {q3['status']:<14} {q3.get('reason', '')}")

    q4 = result['q4']
    lines.append(f"Q4 (corr veto)         : {q4['status']:<14} {q4.get('reason', '')}")
    for ref_entry in q4.get('per_reference', []) or []:
        lines.append(f"   vs {ref_entry['reference_symbol']:<10}: rho={fmt(ref_entry['pearson_rho'])}  n_aligned={ref_entry['n_aligned_returns']}")

    lines.append('')
    lines.append(f"OVERALL                : {result['overall']}")
    if result.get('notes'):
        lines.append(f"notes                  : {'; '.join(result['notes'])}")
    return '\n'.join(lines)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--symbol', required=True, help='candidate pair, e.g. ZECUSDT')
    p.add_argument('--reference-symbols', default='TONUSDT',
                   help='production pair(s) for Q4 correlation veto, comma-separated (default: TONUSDT)')
    p.add_argument('--bt-script', default='backtest.py',
                   help='path to backtest.py')
    p.add_argument('--python', default=sys.executable,
                   help='python interpreter for subprocess')
    p.add_argument('--q1-threshold', type=float, default=DEFAULT_Q1_THRESHOLD_R,
                   help=f'min expectancy_R per window for Q1 PASS (default {DEFAULT_Q1_THRESHOLD_R}; '
                        f'pass 0.05 to align with live decision gate)')
    p.add_argument('--csv-override', default='',
                   help='skip backtest subprocess, use this CSV instead (testing)')
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    # Lazy import: fetch_candles_range needs HTTPS; not all environments allow it.
    try:
        from backtest import fetch_candles_range as fetch_fn
    except ImportError as e:
        print(f"warning: could not import backtest.fetch_candles_range ({e}); Q4 will be INCONCLUSIVE",
              file=sys.stderr)
        fetch_fn = None

    reference_symbols = [s.strip() for s in args.reference_symbols.split(',') if s.strip()]
    result = run_checks(
        args.symbol,
        reference_symbols,
        args.bt_script,
        args.python,
        fetch_fn,
        q1_threshold=args.q1_threshold,
        csv_override=(args.csv_override or None),
    )

    print(_format_human(result), file=sys.stderr)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
