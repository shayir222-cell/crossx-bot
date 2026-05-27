"""Pair selection methodology v2 — Edge Confidence Score (ECS) + greedy selection.

Builds on the audit_pair.py output (summary CSV like audit_top10_20260527.csv)
and applies the formal Selection Methodology defined in
project_pair_selection_methodology.md:

    ECS = 100 * (0.30*E_norm + 0.20*S_norm + 0.20*B_norm
                 + 0.20*M_norm + 0.10*W_norm)

  E_norm: point expectancy in R, saturates at +0.25R
  S_norm: trade count, n/50 capped at 1.0
  B_norm: min(L,S)/max(L,S) — HARD CAP at ECS=50 if either side is 0
  M_norm: Spearman rho on score-bucket means (>=0, capped at 1)
  W_norm: out-of-sample stability across two windows; min/max ratio

Final selection: ECS sorted descending, drop ECS<60, drop adjusted
expectancy < +0.10R (anti-overfit floor), then greedy pick with
correlation veto against already-selected + production pairs.

Usage:
    python audit_pair_v2.py --summary-csv audit_top10_20260527.csv
    python audit_pair_v2.py --summary-csv audit_top10_20260527.csv \\
        --trade-csv backtest_20260514_220735.csv \\
        --window-b-csv audit_top10_20260427.csv \\
        --ref-symbols TONUSDT --max-picks 3

What's optional vs required:
    --summary-csv      : REQUIRED (E/S/B norms come from here; window A)
    --window-b-csv     : optional; enables W_norm (stability across two windows).
                         If missing, W_norm = None and weight rebalances.
    --trade-csv        : optional; enables M_norm (score monotonicity). If missing,
                         M_norm = None and weight rebalances.
    --ref-symbols      : production pair(s); used for correlation veto.

Note on correlation veto: methodology says ">0.70" (signed). This tool uses
|rho| >= 0.70 so anti-correlated pairs also fail the veto -- conservative for
portfolio risk. Document if your operator expects strict signed comparison.

Output:
    JSON   -> stdout
    Human  -> stderr
    bot.py config snippets at end of stderr report
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone


# Methodology constants (from project_pair_selection_methodology.md)
ECS_WEIGHTS = {
    'E_norm': 0.30,
    'S_norm': 0.20,
    'B_norm': 0.20,
    'M_norm': 0.20,
    'W_norm': 0.10,
}
E_SATURATE_R = 0.25       # E_norm = 1.0 when expectancy >= +0.25R
E_FLOOR_R = 0.05          # E_norm = 0 when expectancy <= +0.05R
S_FULL_N = 50             # S_norm = 1.0 when trades >= 50
ECS_MIN_FOR_DEPLOY = 60   # Drop candidates below this
ADJ_EXP_MIN_R = 0.10      # Anti-overfit floor (after slippage subtraction)
B_ZERO_HARD_CAP = 50      # B_norm=0 (one side absent) -> ECS capped at 50
CORRELATION_VETO = 0.70   # Drop if corr with already-picked or prod-pair > 0.70

# Slippage defaults (per-tier) -- methodology Section 3
SLIPPAGE_PCT_PER_SIDE = {
    'top_3':       0.02,  # BTC, ETH, SOL (typical)
    'top_10':      0.05,  # BNB, XRP, ADA, AVAX, DOGE, TRX, TON
    'outside_10':  0.10,  # smaller-cap perps
}
TOP_3 = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}
TOP_10 = {'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOGEUSDT', 'TRXUSDT', 'TONUSDT',
          'HYPEUSDT', 'ZECUSDT'}


def _slippage_pct(symbol):
    if symbol in TOP_3:
        return SLIPPAGE_PCT_PER_SIDE['top_3']
    if symbol in TOP_10:
        return SLIPPAGE_PCT_PER_SIDE['top_10']
    return SLIPPAGE_PCT_PER_SIDE['outside_10']


def _read_summary_csv(path):
    """Read an audit-output CSV (e.g. audit_top10_20260527.csv).

    Required columns: symbol, trades, expectancy_R, long_count, short_count.
    Returns list of dicts (one per row, parsed numerics).
    """
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        required = ('symbol', 'trades', 'expectancy_R', 'long_count', 'short_count')
        for r in required:
            if r not in (reader.fieldnames or []):
                raise ValueError(f'summary CSV missing required column: {r}')
        for r in reader:
            try:
                # best_tp_r is optional: if present and parseable, use it;
                # otherwise leave None and score_candidate defaults to 2.0.
                best_tp_r = None
                btr_raw = r.get('best_tp_r')
                if btr_raw:
                    try:
                        best_tp_r = float(btr_raw)
                    except (ValueError, TypeError):
                        pass
                rows.append({
                    'symbol': r['symbol'],
                    'trades': int(r['trades']),
                    'expectancy_R': float(r['expectancy_R']),
                    'long_count': int(r['long_count']),
                    'short_count': int(r['short_count']),
                    'profit_factor': float(r.get('profit_factor') or 0),
                    'win_rate': float(r.get('win_rate') or 0),
                    'max_dd_pct': float(r.get('max_dd_pct') or 0),
                    'status': r.get('status') or 'OK',
                    'best_tp_r': best_tp_r,
                })
            except (ValueError, TypeError) as e:
                print(f'  [WARN] skip row {r.get("symbol")}: {e}', file=sys.stderr)
    return rows


def _e_norm(expectancy_r):
    """E_norm: saturates at +0.25R, zero at <=+0.05R."""
    if expectancy_r <= E_FLOOR_R:
        return 0.0
    if expectancy_r >= E_SATURATE_R:
        return 1.0
    return (expectancy_r - E_FLOOR_R) / (E_SATURATE_R - E_FLOOR_R)


def _s_norm(trades):
    return min(trades / S_FULL_N, 1.0)


def _b_norm(long_count, short_count):
    """min(L,S) / max(L,S). Zero if either side absent."""
    if long_count == 0 or short_count == 0:
        return 0.0
    return min(long_count, short_count) / max(long_count, short_count)


def _m_norm(spearman_rho):
    """Spearman rho clipped to [0, 1]. None -> None (unavailable)."""
    if spearman_rho is None:
        return None
    return max(0.0, min(1.0, spearman_rho))


def _ecs(e_norm, s_norm, b_norm, m_norm, w_norm):
    """Compute ECS 0-100. Missing components (None) are excluded and the
    remaining weights rebalanced. B_norm=0 caps ECS at 50."""
    components = {
        'E_norm': e_norm, 'S_norm': s_norm,
        'B_norm': b_norm, 'M_norm': m_norm, 'W_norm': w_norm,
    }
    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        return 0.0, {'reason': 'no components available'}
    total_weight = sum(ECS_WEIGHTS[k] for k in available)
    if total_weight == 0:
        return 0.0, {'reason': 'zero total weight'}
    weighted = sum(ECS_WEIGHTS[k] * available[k] for k in available) / total_weight
    ecs = 100.0 * weighted

    # Hard cap when L/S balance signals structural asymmetry
    capped = False
    if b_norm == 0.0:
        if ecs > B_ZERO_HARD_CAP:
            ecs = B_ZERO_HARD_CAP
            capped = True

    return ecs, {
        'components_available': sorted(available.keys()),
        'components_missing': sorted(set(components.keys()) - set(available.keys())),
        'weight_used': total_weight,
        'capped_at_b_zero': capped,
    }


def _adjusted_expectancy(symbol, expectancy_r, sl_atr_mult=2.5, atr_pct_default=0.5):
    """Subtract per-tier slippage from raw expectancy.

    Round-trip slippage in pct = 2 * slippage_per_side. Convert to R:
    cost_R = round_trip_pct / sl_dist_pct, where sl_dist_pct ~= atr_pct * sl_atr_mult.

    Uses atr_pct_default (0.5 = typical 5m liquid perp) as a placeholder
    since we don't have per-symbol ATR here. Conservative for our use.
    """
    slip_per_side = _slippage_pct(symbol)
    round_trip_pct = 2.0 * slip_per_side
    sl_dist_pct = atr_pct_default * sl_atr_mult
    if sl_dist_pct <= 0:
        return expectancy_r
    cost_r = round_trip_pct / sl_dist_pct
    return expectancy_r - cost_r


def _trade_csv_to_spearman(trade_csv_path, symbol):
    """Compute Spearman rho for one symbol from a trade-level CSV.

    Returns (rho_or_None, n_trades, status_string).
    Uses the shared score_buckets module so the answer matches monotonicity.py
    and robustness.py Q2.
    """
    from score_buckets import compute_monotonicity
    rows = []
    try:
        with open(trade_csv_path, newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get('symbol', '').upper() != symbol.upper():
                    continue
                try:
                    rows.append({'score': float(r['score']),
                                 'r_net': float(r['r_net'])})
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError as e:
        return None, 0, f'cannot read trade CSV: {e}'
    if not rows:
        return None, 0, 'no trades for symbol in trade CSV'
    result = compute_monotonicity(rows)
    rho = result.get('spearman_rho')
    return rho, len(rows), result.get('status', '')


def _compute_w_norm(window_a_exp, window_b_exp):
    """W_norm: out-of-sample stability across two windows.

    min(E_a, E_b) / max(E_a, E_b) when both > 0. If either window is
    non-positive (no edge), W_norm=0 (methodology Section 1).
    Returns None if either window's expectancy is missing.
    Both inputs are raw expectancies in R (signed); the gate on >0
    ensures we only ratio positive edges.
    """
    if window_a_exp is None or window_b_exp is None:
        return None
    if window_a_exp <= 0 or window_b_exp <= 0:
        return 0.0
    return min(window_a_exp, window_b_exp) / max(window_a_exp, window_b_exp)


def score_candidate(row, trade_csv_path=None, window_b_exp=None,
                    best_tp_r=None):
    """Build per-candidate scoring dict ready for ECS computation."""
    sym = row['symbol']
    e = _e_norm(row['expectancy_R'])
    s = _s_norm(row['trades'])
    b = _b_norm(row['long_count'], row['short_count'])

    m = None
    m_n = 0
    m_status = 'not computed (no trade CSV)'
    if trade_csv_path:
        rho, n_trades, status = _trade_csv_to_spearman(trade_csv_path, sym)
        m = _m_norm(rho)
        m_n = n_trades
        m_status = status if status else f'spearman rho={rho:+.3f}' if rho is not None else 'rho n/a'

    w = _compute_w_norm(row['expectancy_R'], window_b_exp)
    w_status = 'not computed (no window B CSV)'
    if window_b_exp is not None:
        w_status = f'window_B_exp={window_b_exp:+.3f}R; min/max={w:.3f}' if w is not None else 'window B exp <=0'

    ecs, diag = _ecs(e, s, b, m, w)
    adj_exp = _adjusted_expectancy(sym, row['expectancy_R'])
    return {
        'symbol': sym,
        'expectancy_R': row['expectancy_R'],
        'adjusted_expectancy_R': adj_exp,
        'slippage_pct_per_side': _slippage_pct(sym),
        'trades': row['trades'],
        'long_count': row['long_count'],
        'short_count': row['short_count'],
        'profit_factor': row['profit_factor'],
        'best_tp_r': best_tp_r if best_tp_r is not None else 2.0,
        'E_norm': e,
        'S_norm': s,
        'B_norm': b,
        'M_norm': m,
        'M_status': m_status,
        'M_trades_used': m_n,
        'W_norm': w,
        'W_status': w_status,
        'window_b_expectancy_R': window_b_exp,
        'ECS': ecs,
        'ECS_diag': diag,
    }


def _fetch_correlation_map(symbols, fetch_fn, days=30):
    """For each pair (i,j), Pearson on log returns. Returns dict {(a,b): rho}."""
    from correlation_utils import fetch_closes_map, pair_log_returns_from_maps
    import statistics
    closes = {}
    for sym in symbols:
        m, err = fetch_closes_map(sym, '1H', days, fetch_fn)
        closes[sym] = m if not err else None

    out = {}
    syms = list(symbols)
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            if closes.get(a) is None or closes.get(b) is None:
                continue
            ra, rb, diag = pair_log_returns_from_maps(closes[a], closes[b])
            if len(ra) < 50:
                continue
            try:
                rho = statistics.correlation(ra, rb)
                out[(a, b)] = rho
                out[(b, a)] = rho
            except (statistics.StatisticsError, AttributeError) as e:
                print(f'  [WARN] corr({a},{b}) failed: {e}', file=sys.stderr)
                continue
    return out


def greedy_select(scored, ref_symbols, corr_map, max_picks=3):
    """Sort by ECS desc, drop ECS<60 / adj_exp<floor, greedy with correlation veto.

    Veto uses abs(corr) >= 0.70 (catches both strongly positive and strongly
    negative coupling). Methodology says ">0.70" (signed); tool deviates
    conservatively -- see module docstring.
    """
    eligible = [c for c in scored
                if c['ECS'] >= ECS_MIN_FOR_DEPLOY
                and c['adjusted_expectancy_R'] >= ADJ_EXP_MIN_R]
    eligible.sort(key=lambda c: c['ECS'], reverse=True)

    selected = []
    rejected_corr = []
    for cand in eligible:
        cand_sym = cand['symbol']
        already = [s['symbol'] for s in selected] + list(ref_symbols)
        max_corr = None
        max_corr_with = None
        for other in already:
            if other == cand_sym:
                continue
            key = (cand_sym, other)
            if key in corr_map:
                rho = corr_map[key]
                if max_corr is None or abs(rho) > abs(max_corr):
                    max_corr = rho
                    max_corr_with = other
        if max_corr is not None and abs(max_corr) >= CORRELATION_VETO:
            rejected_corr.append({
                'symbol': cand_sym,
                'reason': f'corr with {max_corr_with}={max_corr:+.3f} >= {CORRELATION_VETO}',
                'rho': max_corr, 'rho_with': max_corr_with,
            })
            continue
        selected.append(cand)
        if len(selected) >= max_picks:
            break

    return selected, eligible, rejected_corr


def _format_human(result):
    lines = []
    lines.append('=' * 70)
    lines.append('  PAIR SELECTION METHODOLOGY v2 -- Edge Confidence Score')
    lines.append('=' * 70)
    lines.append('')
    lines.append(f'  summary CSV    : {result["summary_csv"]}')
    lines.append(f'  trade CSV      : {result.get("trade_csv") or "(none -- M_norm not computed)"}')
    lines.append(f'  window B CSV   : {result.get("window_b_csv") or "(none -- W_norm not computed)"}')
    lines.append(f'  ref symbols    : {", ".join(result["ref_symbols"]) or "(none)"}')
    lines.append(f'  max picks      : {result["max_picks"]}')
    lines.append(f'  slippage       : tier-based (top_3=0.02/side, top_10=0.05/side, '
                 f'outside=0.10/side); atr_pct assumed 0.5 -- adj_exp is approximate')
    lines.append('')
    lines.append('  ECS table (sorted descending):')
    lines.append('')
    header = f'  {"symbol":<10} {"ECS":>6} {"exp_R":>7} {"adj_R":>7} {"E":>5} {"S":>5} {"B":>5} {"M":>5} {"W":>5} {"trades":>6}'
    lines.append(header)
    lines.append(f'  {"-"*10} {"-"*6} {"-"*7} {"-"*7} {"-"*5} {"-"*5} {"-"*5} {"-"*5} {"-"*5} {"-"*6}')

    def fmt(v, n=2, signed=False):
        if v is None:
            return 'n/a'.rjust(5)
        if signed:
            return f'{v:+.{n}f}'
        return f'{v:.{n}f}'

    for c in sorted(result['scored'], key=lambda x: x['ECS'], reverse=True):
        lines.append(
            f'  {c["symbol"]:<10} {c["ECS"]:>6.1f} {c["expectancy_R"]:>+7.3f} '
            f'{c["adjusted_expectancy_R"]:>+7.3f} {fmt(c["E_norm"]):>5} '
            f'{fmt(c["S_norm"]):>5} {fmt(c["B_norm"]):>5} {fmt(c["M_norm"]):>5} '
            f'{fmt(c["W_norm"]):>5} {c["trades"]:>6}'
        )

    lines.append('')
    lines.append(f'  Drop threshold: ECS < {ECS_MIN_FOR_DEPLOY} AND/OR adj_exp_R < {ADJ_EXP_MIN_R:+.2f}')

    eligible = result.get('eligible', [])
    if eligible:
        lines.append('')
        lines.append(f'  ELIGIBLE (passed ECS+adj_exp floors):')
        for c in eligible:
            lines.append(f'    {c["symbol"]:<10} ECS={c["ECS"]:.1f}  adj_exp={c["adjusted_expectancy_R"]:+.3f}R')
    else:
        lines.append('')
        lines.append('  NO ELIGIBLE candidates after floors.')

    rejected = result.get('rejected_by_correlation') or []
    if rejected:
        lines.append('')
        lines.append('  REJECTED BY CORRELATION VETO:')
        for r in rejected:
            lines.append(f'    {r["symbol"]:<10}  {r["reason"]}')

    selected = result.get('selected', [])
    lines.append('')
    if not selected:
        lines.append(f'  FINAL PICKS: NONE -- do not expand production this cycle')
    elif len(selected) < 2:
        lines.append(f'  FINAL PICKS: only {len(selected)} survived -- methodology says do not expand')
        for c in selected:
            lines.append(f'    {c["symbol"]:<10}  ECS={c["ECS"]:.1f}')
    else:
        lines.append(f'  FINAL PICKS ({len(selected)}):')
        for c in selected:
            lines.append(f'    {c["symbol"]:<10}  ECS={c["ECS"]:.1f}  adj_exp={c["adjusted_expectancy_R"]:+.3f}R  trades={c["trades"]}')
        lines.append('')
        lines.append('  Paste into bot.py (after soak window closes):')
        for c in selected:
            tp_r = c.get('best_tp_r', 2.0)
            lines.append(f"    PAIR_TP_R['{c['symbol']}'] = {tp_r}")
            lines.append(f"    PAIR_MIN_SCORE['{c['symbol']}'] = 92")
        lines.append('  NOTE: TP_R defaults to 2.0 unless input CSV provides best_tp_r '
                     'column with a value beating 2.0 by >=0.05R (methodology Section 6).')

    return '\n'.join(lines)


def run(summary_csv, trade_csv, ref_symbols, max_picks, fetch_fn=None,
        window_b_csv=None):
    rows = _read_summary_csv(summary_csv)
    # Window B expectancies (for W_norm) — keyed by symbol
    window_b_exp_map = {}
    if window_b_csv:
        try:
            for row_b in _read_summary_csv(window_b_csv):
                window_b_exp_map[row_b['symbol']] = row_b['expectancy_R']
        except (OSError, ValueError) as e:
            print(f'  [WARN] cannot read window-B CSV: {e}', file=sys.stderr)
    scored = [
        score_candidate(
            r, trade_csv_path=trade_csv,
            window_b_exp=window_b_exp_map.get(r['symbol']),
            best_tp_r=r.get('best_tp_r'))
        for r in rows
    ]

    # Correlation matrix for survivors (only candidates passing ECS>=60)
    survivor_syms = [c['symbol'] for c in scored
                     if c['ECS'] >= ECS_MIN_FOR_DEPLOY
                     and c['adjusted_expectancy_R'] >= ADJ_EXP_MIN_R]
    pair_universe = sorted(set(survivor_syms) | set(ref_symbols))
    corr_map = {}
    if fetch_fn and len(pair_universe) >= 2:
        try:
            corr_map = _fetch_correlation_map(pair_universe, fetch_fn)
        except Exception as e:
            print(f'  [WARN] correlation fetch failed: {e}', file=sys.stderr)

    selected, eligible, rejected_corr = greedy_select(
        scored, ref_symbols, corr_map, max_picks=max_picks)

    return {
        'now_utc': datetime.now(timezone.utc).isoformat(),
        'summary_csv': summary_csv,
        'trade_csv': trade_csv,
        'window_b_csv': window_b_csv,
        'ref_symbols': ref_symbols,
        'max_picks': max_picks,
        'ecs_thresholds': {
            'ECS_MIN_FOR_DEPLOY': ECS_MIN_FOR_DEPLOY,
            'ADJ_EXP_MIN_R': ADJ_EXP_MIN_R,
            'CORRELATION_VETO': CORRELATION_VETO,
        },
        'scored': scored,
        'eligible': eligible,
        'rejected_by_correlation': rejected_corr,
        'selected': selected,
        'correlation_map': {f'{a}|{b}': rho for (a, b), rho in corr_map.items()},
    }


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--summary-csv', required=True,
                   help='audit summary CSV (window A; e.g. audit_top10_20260527.csv)')
    p.add_argument('--trade-csv', default='',
                   help='trade-level CSV for M_norm Spearman computation (optional)')
    p.add_argument('--window-b-csv', default='',
                   help='second-window summary CSV for W_norm stability (optional)')
    p.add_argument('--ref-symbols', default='TONUSDT',
                   help='comma-separated production pairs for correlation veto')
    p.add_argument('--max-picks', type=int, default=3)
    p.add_argument('--no-correlation', action='store_true',
                   help='skip correlation fetch (use when offline / Bitget down)')
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    ref_symbols = [s.strip().upper() for s in args.ref_symbols.split(',') if s.strip()]

    fetch_fn = None
    if not args.no_correlation:
        try:
            from backtest import fetch_candles_range as fetch_fn  # noqa: F401
        except ImportError as e:
            print(f'  [WARN] cannot import backtest.fetch_candles_range: {e}', file=sys.stderr)
            fetch_fn = None

    try:
        result = run(
            args.summary_csv,
            args.trade_csv or None,
            ref_symbols,
            args.max_picks,
            fetch_fn=fetch_fn,
            window_b_csv=args.window_b_csv or None,
        )
    except (OSError, ValueError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    print(_format_human(result), file=sys.stderr)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
