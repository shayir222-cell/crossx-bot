"""Pairwise log-return correlation matrix for a list of trading symbols.

Fetches 1H candles for each symbol once (N fetches, not N^2), then computes
Pearson correlation on log returns for every (i,j) pair where i<j. The
matrix is symmetric with None on the diagonal.

Used by the pair selection methodology to detect redundant production
picks (e.g. BTC+ETH correlate >0.85 on 1H; deploying both gives no
diversification).

Usage:
    python correlation_matrix.py --symbols TONUSDT,ZECUSDT,HYPEUSDT,DOGEUSDT,BNBUSDT
    python correlation_matrix.py --symbols BTC,ETH,SOL --days 60 --threshold 0.70

Output:
    correlation_matrix_YYYYMMDD_HHMMSS.csv     -> NxN matrix on disk
    JSON to stdout                             -> matrix + flagged pairs
    Human summary to stderr                    -> table preview + warnings

Threshold filtering uses |rho| (not rho) so strongly negative correlations
also surface as portfolio-risk concerns.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone

from correlation_utils import fetch_closes_map, pair_log_returns_from_maps


DEFAULT_DAYS = 30
DEFAULT_THRESHOLD = 0.70
MIN_ALIGNED_RETURNS = 50
INTER_SYMBOL_SLEEP_SEC = 0.5  # extra rate-limit cushion beyond fetch_candles_range's own sleep


def _utc_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')


def _dedupe_preserve_order(symbols):
    """Uppercase, strip, drop empties, preserve first-occurrence order."""
    seen = set()
    out = []
    for s in symbols:
        s = (s or '').strip().upper()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _fisher_z_ci(rho, n, confidence=0.95):
    """Fisher z-transform CI on Pearson rho. Returns (lo, hi) or (None, None).

    z = 0.5 * ln((1+rho)/(1-rho));  SE(z) = 1/sqrt(n-3)
    Then transform CI bounds back to rho-space via tanh.
    Cheap, no bootstrap, accurate for moderately heavy-tailed iid data.
    """
    if rho is None or n < 4 or abs(rho) >= 1.0:
        return None, None
    z = 0.5 * math.log((1.0 + rho) / (1.0 - rho))
    se = 1.0 / math.sqrt(n - 3)
    z_crit = statistics.NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    z_lo = z - z_crit * se
    z_hi = z + z_crit * se
    rho_lo = (math.exp(2 * z_lo) - 1) / (math.exp(2 * z_lo) + 1)
    rho_hi = (math.exp(2 * z_hi) - 1) / (math.exp(2 * z_hi) + 1)
    return rho_lo, rho_hi


def _fetch_all(symbols, days, fetch_fn):
    """Pre-fetch closes map for each symbol once. Returns dict {sym: (map, err)}."""
    results = {}
    for i, sym in enumerate(symbols):
        print(f'  fetch {i+1}/{len(symbols)}: {sym}', file=sys.stderr)
        m, err = fetch_closes_map(sym, '1H', days, fetch_fn)
        results[sym] = (m, err)
        if err:
            print(f'    -> error: {err}', file=sys.stderr)
        else:
            print(f'    -> {len(m)} 1H candles', file=sys.stderr)
        # Cushion between symbols (fetch_candles_range already sleeps between batches)
        if i < len(symbols) - 1:
            time.sleep(INTER_SYMBOL_SLEEP_SEC)
    return results


def _compute_matrix(symbols, fetched, min_aligned=MIN_ALIGNED_RETURNS):
    """Build NxN matrix from pre-fetched closes maps. Symmetric, None on diagonal.

    Returns (matrix, cell_diag). Each cell diag includes Fisher-z 95% CI
    on rho so operators can judge boundary cases without re-running.
    """
    n = len(symbols)
    matrix = [[None] * n for _ in range(n)]
    cell_diag = {}
    for i in range(n):
        sym_i = symbols[i]
        map_i, err_i = fetched[sym_i]
        for j in range(i + 1, n):
            sym_j = symbols[j]
            map_j, err_j = fetched[sym_j]
            if err_i or err_j:
                cell_diag[(i, j)] = {
                    'status': 'null',
                    'reason': f'fetch errors: a={err_i}, b={err_j}',
                }
                continue
            ra, rb, diag = pair_log_returns_from_maps(map_i, map_j)
            if len(ra) < min_aligned:
                cell_diag[(i, j)] = {
                    'status': 'null',
                    'reason': f'too few aligned returns: {len(ra)}',
                    'n_common_candles': diag.get('n_common_candles', 0),
                    'n_gap_dropped': diag.get('n_gap_dropped', 0),
                }
                continue
            try:
                rho = statistics.correlation(ra, rb)
            except (statistics.StatisticsError, AttributeError) as e:
                cell_diag[(i, j)] = {'status': 'null',
                                     'reason': f'pearson failed: {e}'}
                continue
            ci_lo, ci_hi = _fisher_z_ci(rho, len(ra), confidence=0.95)
            matrix[i][j] = rho
            matrix[j][i] = rho
            cell_diag[(i, j)] = {
                'status': 'ok',
                'rho': rho,
                'rho_ci_95': [ci_lo, ci_hi],
                'n_aligned_returns': len(ra),
                'n_common_candles': diag['n_common_candles'],
                'n_gap_dropped': diag['n_gap_dropped'],
            }
    return matrix, cell_diag


def _pairs_over_threshold(symbols, matrix, threshold, mode='abs'):
    """Return list of {a, b, rho} dicts sorted by |rho| desc.

    mode='abs'    : flag |rho| >= threshold (catches anti-correlation too)
    mode='signed' : flag rho >= threshold only (matches methodology doc verbatim)
    """
    out = []
    n = len(symbols)
    for i in range(n):
        for j in range(i + 1, n):
            rho = matrix[i][j]
            if rho is None:
                continue
            keep = (abs(rho) >= threshold) if mode == 'abs' else (rho >= threshold)
            if keep:
                out.append({'a': symbols[i], 'b': symbols[j], 'rho': rho})
    out.sort(key=lambda x: abs(x['rho']), reverse=True)
    return out


def _write_matrix_csv(symbols, matrix, path):
    """Write symmetric matrix with header row + leading symbol column.

    Diagonal is written as 1.0000 (the self-correlation by definition) so
    naive consumers can distinguish "self" from "couldn't compute" (empty).
    """
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([''] + symbols)
        for i, sym in enumerate(symbols):
            row = [sym]
            for j in range(len(symbols)):
                if i == j:
                    row.append('1.0000')
                elif matrix[i][j] is None:
                    row.append('')  # blank => not computed (NaN to pandas)
                else:
                    row.append(f'{matrix[i][j]:.4f}')
            w.writerow(row)


def _format_human(symbols, matrix, pairs_abs, pairs_signed, threshold, csv_path, fetch_summary):
    """fetch_summary: dict {sym: (n_candles_or_None, error_or_None)}."""
    lines = []
    lines.append(f'=== Correlation matrix ({len(symbols)} symbols, threshold {threshold}) ===')
    n_failed = sum(1 for sym in symbols if fetch_summary.get(sym, (None, None))[1])
    if n_failed:
        lines.append(f'WARNING: {n_failed} symbols failed to fetch -- see per-symbol notes')
    lines.append('')
    # Compact matrix preview (truncate long symbol names to 8 chars)
    short = [s[:8] for s in symbols]
    header_row = ' ' * 9 + ' '.join(f'{s:>8}' for s in short)
    lines.append(header_row)
    for i, sym in enumerate(short):
        row_vals = []
        for j in range(len(symbols)):
            v = matrix[i][j]
            if v is None:
                row_vals.append(f'{"--":>8}')
            else:
                row_vals.append(f'{v:>+8.3f}')
        lines.append(f'{sym:>8} ' + ' '.join(row_vals))
    lines.append('')
    if pairs_signed:
        lines.append(f'Pairs with signed rho >= {threshold} (methodology veto):')
        for p in pairs_signed:
            lines.append(f"  {p['a']} <-> {p['b']}: rho={p['rho']:+.3f}")
    if pairs_abs:
        abs_only = [p for p in pairs_abs if p['rho'] < threshold]
        if abs_only:
            lines.append(f'Additional pairs with |rho| >= {threshold} (anti-correlated):')
            for p in abs_only:
                lines.append(f"  {p['a']} <-> {p['b']}: rho={p['rho']:+.3f}")
    if not pairs_signed and not pairs_abs:
        lines.append(f'No pairs at |rho| >= {threshold} (diversified universe).')
    lines.append('')
    lines.append(f'Matrix CSV: {csv_path}')
    return '\n'.join(lines)


def run(symbols, days, threshold, fetch_fn, out_dir='.'):
    """Top-level: fetch all, compute matrix, write CSV, return result dict."""
    symbols = _dedupe_preserve_order(symbols)
    result = {
        'symbols': symbols,
        'days': days,
        'threshold': threshold,
        'now_utc': datetime.now(timezone.utc).isoformat(),
        'matrix': [],
        'pairs_over_threshold_abs': [],
        'pairs_over_threshold_signed': [],
        'csv_path': None,
        'per_symbol_fetch': {},
        'cell_diag': [],
        'warnings': [],
    }
    if len(symbols) < 2:
        result['warnings'].append('need at least 2 symbols to build a matrix')
        return result

    fetched = _fetch_all(symbols, days, fetch_fn)
    for sym in symbols:
        m, err = fetched[sym]
        result['per_symbol_fetch'][sym] = {
            'n_candles': len(m) if m else 0,
            'error': err,
        }
        if err:
            result['warnings'].append(f'{sym}: {err}')

    matrix, cell_diag = _compute_matrix(symbols, fetched)
    result['matrix'] = matrix
    # Serialize cell diagnostics as a list (tuples can't be JSON keys)
    result['cell_diag'] = [
        {'i': i, 'j': j, 'a': symbols[i], 'b': symbols[j], **diag}
        for (i, j), diag in sorted(cell_diag.items())
    ]
    n_null_cells = sum(1 for d in result['cell_diag'] if d.get('status') == 'null')
    if n_null_cells:
        result['warnings'].append(f'{n_null_cells} pair cells could not be computed')

    csv_path = os.path.join(out_dir, f'correlation_matrix_{_utc_stamp()}.csv')
    _write_matrix_csv(symbols, matrix, csv_path)
    result['csv_path'] = csv_path
    result['pairs_over_threshold_abs'] = _pairs_over_threshold(symbols, matrix, threshold, mode='abs')
    result['pairs_over_threshold_signed'] = _pairs_over_threshold(symbols, matrix, threshold, mode='signed')
    return result


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--symbols', required=True,
                   help='comma-separated symbols, e.g. TONUSDT,ZECUSDT,HYPEUSDT')
    p.add_argument('--days', type=int, default=DEFAULT_DAYS,
                   help=f'lookback window in days (default {DEFAULT_DAYS})')
    p.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD,
                   help=f'flag pairs with |rho| >= this (default {DEFAULT_THRESHOLD})')
    p.add_argument('--out-dir', default='.', help='directory for matrix CSV output')
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if args.days <= 0:
        print('error: --days must be positive', file=sys.stderr)
        return 2
    if not (0.0 <= args.threshold <= 1.0):
        print('error: --threshold must be in [0.0, 1.0]', file=sys.stderr)
        return 2

    raw_symbols = [s for s in args.symbols.split(',')]
    symbols = _dedupe_preserve_order(raw_symbols)
    if len(symbols) < 2:
        print(f'error: need at least 2 distinct symbols (got {symbols})', file=sys.stderr)
        return 2
    if len(symbols) < len(raw_symbols):
        print(f'note: deduped {len(raw_symbols)} -> {len(symbols)} symbols', file=sys.stderr)

    try:
        from backtest import fetch_candles_range as fetch_fn
    except ImportError as e:
        print(f'error: cannot import backtest.fetch_candles_range: {e}', file=sys.stderr)
        return 1

    result = run(symbols, args.days, args.threshold, fetch_fn, out_dir=args.out_dir)
    fetch_summary = {s: (result['per_symbol_fetch'][s].get('n_candles'),
                         result['per_symbol_fetch'][s].get('error')) for s in symbols}
    print(_format_human(symbols, result['matrix'],
                        result['pairs_over_threshold_abs'],
                        result['pairs_over_threshold_signed'],
                        args.threshold, result['csv_path'], fetch_summary),
          file=sys.stderr)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
