"""Score-bucket monotonicity analyzer for existing backtest CSVs.

Reads one or more backtest CSVs and reports, per (csv, symbol), how
expectancy varies across score buckets [92,94), [94,96), [96,98), [98,101).
Spearman rho between bucket midpoint and bucket-mean r_net classifies
the pair as PASS (rho>=0.30), GRAY ([0, 0.30)), FAIL (<0), or
INCONCLUSIVE (<3 non-empty buckets). When only 3 buckets are non-empty
AND |rho|<0.70, FAIL/PASS are softened to GRAY (Spearman on 3 points is
a discrete-noise statistic at non-extreme |rho| values).

This is the standalone analyzer counterpart to robustness.py's Q2 gate.
Both delegate to score_buckets.compute_monotonicity for the actual math
so they cannot drift.

Usage:
    python monotonicity.py --csv backtest_20260514_220735.csv --all-symbols
    python monotonicity.py --csv backtest_20260514_220735.csv --symbol TONUSDT
    python monotonicity.py --csv-glob "audit_runs/*.csv" --all-symbols

Output:
    JSON   -> stdout  {reports: [{csv, symbol, status, spearman_rho, ...}, ...]}
    Human  -> stderr  table preview + warnings
Exit code: 0 if at least one report produced; 2 if no inputs / file not found.
"""
import argparse
import csv
import glob
import json
import sys
from datetime import datetime, timezone

from score_buckets import compute_monotonicity, SCORE_BUCKETS


def _load_csv(csv_path):
    """Read backtest CSV, return (rows_by_symbol, errors).

    rows_by_symbol: dict {symbol: [{'score': int, 'r_net': float}, ...]}
    errors: dict with counters (skipped_no_rnet, skipped_no_score,
            n_score_out_of_range, missing_columns_msg)
    Returns (None, {'error': msg}) on hard failure (missing file, etc).
    """
    if not csv_path:
        return None, {'error': 'no csv path'}
    rows_by_symbol = {}
    errors = {
        'skipped_no_rnet': 0,
        'skipped_no_score': 0,
        'n_score_out_of_range': 0,
        'rows_total': 0,
        'missing_required_column': None,
    }
    try:
        # utf-8-sig handles BOM if a CSV was written by an Excel-style tool
        f = open(csv_path, newline='', encoding='utf-8-sig')
    except OSError as e:
        return None, {'error': f'cannot open: {e}'}

    with f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        for required in ('symbol', 'score', 'r_net'):
            if required not in cols:
                errors['missing_required_column'] = required
                return None, errors
        score_min = SCORE_BUCKETS[0][0]
        score_max = SCORE_BUCKETS[-1][1]
        for row in reader:
            errors['rows_total'] += 1
            sym = (row.get('symbol') or '').strip().upper()
            if not sym:
                continue
            score_raw = row.get('score')
            r_net_raw = row.get('r_net')
            if score_raw is None or score_raw == '':
                errors['skipped_no_score'] += 1
                continue
            if r_net_raw is None or r_net_raw == '':
                errors['skipped_no_rnet'] += 1
                continue
            try:
                score = float(score_raw)
                r_net = float(r_net_raw)
            except (ValueError, TypeError):
                errors['skipped_no_rnet'] += 1
                continue
            if not (score_min <= score < score_max):
                errors['n_score_out_of_range'] += 1
                continue
            rows_by_symbol.setdefault(sym, []).append(
                {'score': score, 'r_net': r_net})
    return rows_by_symbol, errors


def analyze_csv(csv_path, symbols_filter=None):
    """Return list of reports for one CSV, one per symbol (filtered)."""
    rows_by_symbol, errors = _load_csv(csv_path)
    if rows_by_symbol is None:
        reason = errors.get('error') or f'missing column {errors.get("missing_required_column")}'
        return [{'csv': csv_path, 'symbol': None, 'status': 'error',
                 'reason': reason, 'errors': errors}]

    reports = []
    syms = sorted(rows_by_symbol.keys())
    if symbols_filter:
        syms = [s for s in syms if s in symbols_filter]
        missing = set(symbols_filter) - set(rows_by_symbol.keys())
        for m in sorted(missing):
            reports.append({
                'csv': csv_path, 'symbol': m, 'status': 'INCONCLUSIVE',
                'reason': f'no trades for {m} in {csv_path}',
                'n_trades': 0, 'errors': errors,
            })

    for sym in syms:
        trades = rows_by_symbol[sym]
        result = compute_monotonicity(trades)
        reports.append({
            'csv': csv_path,
            'symbol': sym,
            'status': result['status'],
            'spearman_rho': result['spearman_rho'],
            'reason': result.get('reason'),
            'caveat': result.get('caveat'),
            'buckets': result['buckets'],
            'n_trades': len(trades),
            'errors': errors,
        })
    return reports


def _format_human(reports):
    lines = []
    lines.append(f'=== Score-bucket monotonicity ({len(reports)} reports) ===')
    by_csv = {}
    for r in reports:
        by_csv.setdefault(r['csv'], []).append(r)
    for csv_path, group in by_csv.items():
        lines.append('')
        lines.append(f'--- {csv_path} ---')
        for r in group:
            if r.get('status') == 'error':
                lines.append(f'  ERROR: {r.get("reason")}')
                continue
            sym = r['symbol']
            stat = r['status']
            rho = r.get('spearman_rho')
            n = r.get('n_trades', 0)
            rho_str = f'{rho:+.3f}' if rho is not None else 'n/a'
            lines.append(f'  {sym:<12} {stat:<14} rho={rho_str}  n={n}')
            if r.get('caveat'):
                lines.append(f'               caveat: {r["caveat"]}')
            for b in r.get('buckets') or []:
                m = b.get('mean_r_net')
                m_str = f'{m:+.3f}' if m is not None else 'n/a'
                lines.append(f'    bucket {b["label"]:<8} n={b["n"]:<4} mean_r_net={m_str}')
    return '\n'.join(lines)


def _resolve_csv_inputs(args):
    """Build the list of CSV paths from --csv / --csv-glob args."""
    csv_paths = []
    if args.csv:
        csv_paths.append(args.csv)
    if args.csv_glob:
        matches = sorted(p for p in glob.glob(args.csv_glob) if p.lower().endswith('.csv'))
        if not matches:
            return None, f'--csv-glob {args.csv_glob!r} matched no .csv files'
        csv_paths.extend(matches)
    if not csv_paths:
        return None, 'no csv inputs (use --csv or --csv-glob)'
    # dedupe preserving order
    seen = set()
    out = []
    for p in csv_paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, None


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    src = p.add_argument_group('csv inputs')
    src.add_argument('--csv', default='', help='single backtest CSV path')
    src.add_argument('--csv-glob', default='',
                     help='glob pattern, e.g. "audit_runs/*.csv"')
    sel = p.add_argument_group('symbol selection')
    sel.add_argument('--symbol', default='',
                     help='comma-separated symbols to analyze (else --all-symbols)')
    sel.add_argument('--all-symbols', action='store_true',
                     help='analyze every symbol present in each CSV')
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    csv_paths, err = _resolve_csv_inputs(args)
    if err:
        print(f'error: {err}', file=sys.stderr)
        return 2

    symbols_filter = None
    if args.symbol:
        symbols_filter = [s.strip().upper() for s in args.symbol.split(',') if s.strip()]
    elif not args.all_symbols:
        print('warning: neither --symbol nor --all-symbols given; defaulting to --all-symbols',
              file=sys.stderr)

    all_reports = []
    for path in csv_paths:
        all_reports.extend(analyze_csv(path, symbols_filter=symbols_filter))

    out = {
        'now_utc': datetime.now(timezone.utc).isoformat(),
        'csv_paths': csv_paths,
        'symbols_filter': symbols_filter,
        'reports': all_reports,
    }
    print(_format_human(all_reports), file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))
    return 0 if all_reports else 2


if __name__ == '__main__':
    sys.exit(main())
