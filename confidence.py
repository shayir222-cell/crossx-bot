"""Bootstrap confidence intervals on live trading edge.

Computes a percentile-bootstrap CI on expectancy (R-multiples) or profit
factor (PF), plus a one-sided 95% lower bound for the expectancy gate
decision, plus a chi-square CI on σ for the Wald N-needed estimate.

Used to decide whether the Phase 0 -> Phase 1 gate (live expectancy >= +0.05R)
is supported by the data, or whether the sample is still too small.

R is not stored — derived per-trade as:
    sl_dist_pct = (atr * SL_ATR_MULT / entry) * 100   # both in percent
    r           = pnl_pct / sl_dist_pct
Trades with NULL/zero atr or entry are skipped (count reported).

For PF, the bootstrap is run in log space (log-PF transform) — the percentile
CI on log-PF, exponentiated, is much better-behaved for heavy-tailed ratio
statistics than naive percentile on PF directly.

Usage:
    python confidence.py                                   # all symbols, all-time, expectancy
    python confidence.py --symbol TONUSDT --days 30
    python confidence.py --symbol TONUSDT --metric pf
    python confidence.py --bootstrap-iters 20000 --seed 42

Output convention:
    JSON   -> stdout  (machine consumption: pipe to jq, parse in scripts)
    Human  -> stderr  (table-formatted summary)
Exit code is 0 even at N=0; check the JSON "status" field.

Status vocabulary:
    insufficient            n < 5  (refuse to bootstrap)
    ok                      n >= 10
    ok + warning            5 <= n < 10 or 10 <= n < 30 (see "warnings" list)
"""
import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_DB = os.environ.get('DB_PATH', './crossx.db')
DEFAULT_SL_ATR_MULT = float(os.environ.get('SL_ATR_MULT', '2.5'))
DEFAULT_TARGET_HALFWIDTH_R = 0.10
PF_UNDEFINED_WARN_THRESHOLD = 0.01  # warn when >1% of PF resamples are undefined


def _z_value(confidence):
    """Two-sided z critical value for given confidence (e.g. 0.95 -> 1.96)."""
    alpha = 1.0 - confidence
    return statistics.NormalDist().inv_cdf(1.0 - alpha / 2.0)


def _parse_since(spec):
    """'24h' / '7d' / '30d' / '4w' -> ISO timestamp string.

    Returns:
        (timestamp_str_or_None, ok_bool). ok=False means the user passed a
        non-empty spec that failed to parse — caller should warn.
        Empty/None spec returns (None, True) — all-time is a valid intent.
    """
    if not spec:
        return None, True
    s = spec.strip().lower()
    try:
        if s.endswith('h'):
            qty = int(s[:-1])
            unit = timedelta(hours=qty)
        elif s.endswith('d'):
            qty = int(s[:-1])
            unit = timedelta(days=qty)
        elif s.endswith('w'):
            qty = int(s[:-1])
            unit = timedelta(weeks=qty)
        else:
            return None, False
        if qty <= 0:
            return None, False
        dt = datetime.now(timezone.utc) - unit
        return dt.strftime('%Y-%m-%d %H:%M:%S'), True
    except (ValueError, TypeError):
        return None, False


def _fetch_trades(conn, since_ts, symbol):
    """Read closed analytics_trades, filtered by optional since/symbol.

    Returns list of dicts with the columns needed for R derivation.
    Filters NULL pnl_pct and NULL ts_close_utc at the SQL layer.
    """
    sql = (
        'SELECT symbol, side, pnl_pct, won, exit_reason, atr, entry, exit, sl, '
        '       ts_close_utc, score, tp1_hit '
        '  FROM analytics_trades '
        ' WHERE ts_close_utc IS NOT NULL '
        '   AND pnl_pct IS NOT NULL'
    )
    params = []
    if since_ts:
        sql += ' AND ts_close_utc >= ?'
        params.append(since_ts)
    if symbol and symbol.lower() != 'all':
        sql += ' AND symbol = ?'
        params.append(symbol)
    sql += ' ORDER BY ts_close_utc'
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _derive_r_multiples(trades, sl_atr_mult):
    """Compute R-multiples for trades with valid atr+entry.

    Returns (r_list, pnl_list, skipped_count). pnl_list is parallel to r_list
    and preserves percent units; used for PF computation. Both lists are in
    the same order, so the bootstrap of either metric resamples the same
    trade indices (when seeded identically).

    NOTE on partial exits / trails: pnl_pct is the *blended* realized P&L,
    while sl_dist_pct is the *original* 1R risk. R-multiples for trailed
    winners and stop-outs come from structurally different generative
    processes; the bootstrap treats them as iid. This is the conventional
    definition but worth knowing when interpreting the CI.
    """
    r_list = []
    pnl_list = []
    skipped = 0
    for t in trades:
        atr = t.get('atr')
        entry = t.get('entry')
        pnl_pct = t.get('pnl_pct')
        if atr is None or entry is None or pnl_pct is None:
            skipped += 1
            continue
        try:
            atr_f = float(atr)
            entry_f = float(entry)
            pnl_f = float(pnl_pct)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if atr_f <= 0 or entry_f <= 0:
            skipped += 1
            continue
        sl_dist_pct = (atr_f * sl_atr_mult / entry_f) * 100.0
        if sl_dist_pct <= 0:
            skipped += 1
            continue
        r = pnl_f / sl_dist_pct
        r_list.append(r)
        pnl_list.append(pnl_f)
    return r_list, pnl_list, skipped


def _profit_factor(pnls):
    """PF in pnl_pct units. Returns None when undefined.

    Convention: p > 0 is a win, p <= 0 is a loss (matches analyze.py:92).
    Zero-PnL exits (true BE) contribute 0 to both numerator and denominator
    so they don't affect PF numerically, but they count toward "loss bucket".
    """
    if not pnls:
        return None
    wins = sum(p for p in pnls if p > 0)
    losses = sum(p for p in pnls if p <= 0)
    abs_loss = abs(losses)
    if abs_loss == 0:
        return None
    if wins == 0:
        return 0.0
    return wins / abs_loss


def _log_pf(pnls):
    """log(PF). Returns None when PF is undefined or <= 0.

    Used for percentile-bootstrap CI on PF in log space, which is
    dramatically better-behaved for heavy-tailed ratio statistics than
    a direct percentile-bootstrap CI on PF.
    """
    pf = _profit_factor(pnls)
    if pf is None or pf <= 0:
        return None
    return math.log(pf)


def _expectancy(values):
    if not values:
        return None
    return sum(values) / len(values)


def _bootstrap(values, iters, rng, statistic):
    """Resample with replacement; collect statistic over iters.

    statistic: callable taking a list -> scalar or None. None values are
    counted (undefined_count) but excluded from the result list.
    """
    n = len(values)
    if n == 0:
        return [], 0
    results = []
    undefined = 0
    for _ in range(iters):
        sample = rng.choices(values, k=n)
        s = statistic(sample)
        if s is None:
            undefined += 1
        else:
            results.append(s)
    return results, undefined


def _percentile(sorted_values, q):
    """Linear-interpolated percentile from a sorted list. q in [0, 1]."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo_i = int(math.floor(pos))
    hi_i = int(math.ceil(pos))
    if lo_i == hi_i:
        return sorted_values[lo_i]
    frac = pos - lo_i
    return sorted_values[lo_i] * (1 - frac) + sorted_values[hi_i] * frac


def _ci(bootstrap_values, confidence):
    if not bootstrap_values:
        return None, None
    sorted_vals = sorted(bootstrap_values)
    alpha = 1.0 - confidence
    lo = _percentile(sorted_vals, alpha / 2.0)
    hi = _percentile(sorted_vals, 1.0 - alpha / 2.0)
    return lo, hi


def _one_sided_lower(bootstrap_values, confidence):
    """One-sided lower bound at the given confidence (e.g. 95% LB)."""
    if not bootstrap_values:
        return None
    sorted_vals = sorted(bootstrap_values)
    return _percentile(sorted_vals, 1.0 - confidence)


def _chi2_quantile(p, df):
    """Wilson-Hilferty approximation to chi-square quantile.

    chi2_{p, df} ≈ df * (1 - 2/(9*df) + z_p * sqrt(2/(9*df)))^3

    Accurate to ~1% for df >= 5; degrades for very small df. Returns None
    if df < 2 (where σ CI is meaningless anyway).
    """
    if df < 2:
        return None
    try:
        z = statistics.NormalDist().inv_cdf(p)
    except statistics.StatisticsError:
        return None
    h = 2.0 / (9.0 * df)
    return df * (1.0 - h + z * math.sqrt(h)) ** 3


def _sigma_ci(sample_stdev, n, confidence):
    """Confidence interval on the population σ given sample stdev.

    Uses the chi-square pivot: (N-1)*s^2 / σ^2 ~ χ²_{N-1}.
    Solving for σ:
        σ_lo = s * sqrt((N-1) / χ²_high)
        σ_hi = s * sqrt((N-1) / χ²_low)

    Wilson-Hilferty approximation degrades at df<5 (i.e. n<6): ~5% error in
    tails. At n=5 the σ CI is wide enough that the WH error is dominated by
    sample noise anyway, but flag it in caller output.
    Returns (sigma_lo, sigma_hi) or (None, None) if N too small.
    """
    if sample_stdev is None or sample_stdev <= 0 or n < 2:
        return None, None
    df = n - 1
    alpha = 1.0 - confidence
    chi_hi = _chi2_quantile(1.0 - alpha / 2.0, df)
    chi_lo = _chi2_quantile(alpha / 2.0, df)
    if chi_hi is None or chi_lo is None or chi_hi <= 0 or chi_lo <= 0:
        return None, None
    sigma_lo = sample_stdev * math.sqrt(df / chi_hi)
    sigma_hi = sample_stdev * math.sqrt(df / chi_lo)
    return sigma_lo, sigma_hi


def _wald_n_needed(stdev, target_halfwidth, current_n, confidence):
    """Approximate N to reach target CI half-width for a mean.

    N = (z(c) * σ / target)². Uses the requested confidence level.
    """
    if stdev is None or stdev <= 0 or target_halfwidth <= 0:
        return None, None
    z = _z_value(confidence)
    n_total = math.ceil((z * stdev / target_halfwidth) ** 2)
    additional = max(0, n_total - current_n)
    return n_total, additional


def _classify_sample(n, metric):
    """Map sample size to (status, warnings).

    Expectancy (mean of R) — Wald CLT thresholds:
        < 5   -> insufficient (refuse to bootstrap)
        5-9   -> ok + small_sample warning
        10-29 -> ok + Wald CLT unreliable warning
        >=30  -> ok
    PF (log-ratio) — heavier tails, want larger N:
        < 5   -> insufficient
        5-9   -> ok + small_sample warning
        10-49 -> ok + pf_small_sample_n_under_50 warning
        >=50  -> ok
    """
    if n < 5:
        return 'insufficient_small_sample', ['n_under_5']
    warnings = []
    if n < 10:
        warnings.append('small_sample_n_under_10')
    if metric == 'expectancy_r' and 10 <= n < 30:
        warnings.append('wald_clt_unreliable_n_under_30')
    if metric == 'pf' and 10 <= n < 50:
        warnings.append('pf_small_sample_n_under_50')
    # Wilson-Hilferty σ-CI degrades at df<5; only matters for expectancy
    # which uses _sigma_ci. PF doesn't use σ CI, so suppress this warning.
    if n < 6 and metric == 'expectancy_r':
        warnings.append('wilson_hilferty_approx_degrades_at_df_under_5')
    return 'ok', warnings


_ALL_RESULT_KEYS = (
    'point_estimate', 'ci_lo', 'ci_hi', 'ci_arm_lower', 'ci_arm_upper',
    'one_sided_lb', 'one_sided_confidence', 'bootstrap_median',
    'bootstrap_stderr', 'bootstrap_stderr_log', 'recommended_additional_n',
    'n_needed_total', 'n_needed_lo', 'n_needed_hi',
    'sample_stdev_r', 'sample_stdev_r_ci',
    'n_bootstrap_pf_undefined', 'n_needed_reason',
)


def compute(trades, metric, sl_atr_mult, bootstrap_iters, confidence,
            seed, target_halfwidth):
    """Top-level compute. Returns a result dict ready for JSON serialization.

    Output schema is uniform across all paths — every key in _ALL_RESULT_KEYS
    is present (possibly None). Callers can rely on stable keyset.
    """
    r_list, pnl_list, skipped = _derive_r_multiples(trades, sl_atr_mult)
    n = len(r_list)
    warnings = []
    out = {
        'metric': metric,
        'n_trades_total': len(trades),
        'n_trades_used': n,
        'n_skipped_no_atr_or_entry': skipped,
        'bootstrap_iters': bootstrap_iters,
        'confidence': confidence,
        'seed': seed,
        'sl_atr_mult': sl_atr_mult,
        'target_halfwidth_r': target_halfwidth,
    }
    for k in _ALL_RESULT_KEYS:
        out[k] = None

    if len(trades) > 0:
        skip_frac = skipped / len(trades)
        if skip_frac > 0.05:
            warnings.append(f'skipped_fraction_high_{skip_frac:.1%}')

    if n == 0:
        out['status'] = 'insufficient'
        out['warnings'] = warnings + ['no_usable_trades']
        return out

    sample_status, sample_warnings = _classify_sample(n, metric)
    warnings.extend(sample_warnings)
    if sample_status.startswith('insufficient'):
        out['status'] = sample_status
        out['warnings'] = warnings
        return out

    rng = random.Random(seed)

    out['one_sided_confidence'] = confidence

    if metric == 'expectancy_r':
        point = _expectancy(r_list)
        boot, _undefined = _bootstrap(r_list, bootstrap_iters, rng, _expectancy)
        lo, hi = _ci(boot, confidence)
        one_sided_lb = _one_sided_lower(boot, confidence)
        out['point_estimate'] = point
        out['ci_lo'] = lo
        out['ci_hi'] = hi
        out['one_sided_lb'] = one_sided_lb
        out['ci_arm_lower'] = (point - lo) if (point is not None and lo is not None) else None
        out['ci_arm_upper'] = (hi - point) if (point is not None and hi is not None) else None
        out['bootstrap_median'] = statistics.median(boot) if boot else None
        out['bootstrap_stderr'] = statistics.stdev(boot) if len(boot) >= 2 else None

        sample_stdev = statistics.stdev(r_list) if n >= 2 else None
        sigma_lo, sigma_hi = _sigma_ci(sample_stdev, n, confidence)
        n_total, additional = _wald_n_needed(sample_stdev, target_halfwidth, n, confidence)
        n_total_hi, _ = _wald_n_needed(sigma_hi, target_halfwidth, n, confidence)
        n_total_lo, _ = _wald_n_needed(sigma_lo, target_halfwidth, n, confidence)
        out['sample_stdev_r'] = sample_stdev
        out['sample_stdev_r_ci'] = [sigma_lo, sigma_hi]
        out['n_needed_total'] = n_total
        out['n_needed_lo'] = n_total_lo
        out['n_needed_hi'] = n_total_hi
        out['recommended_additional_n'] = additional

    elif metric == 'pf':
        point = _profit_factor(pnl_list)
        # Bootstrap in log space — better for heavy-tailed ratio
        boot_log, undefined = _bootstrap(pnl_list, bootstrap_iters, rng, _log_pf)
        lo_log, hi_log = _ci(boot_log, confidence)
        one_sided_lb_log = _one_sided_lower(boot_log, confidence)
        lo = math.exp(lo_log) if lo_log is not None else None
        hi = math.exp(hi_log) if hi_log is not None else None
        one_sided_lb = math.exp(one_sided_lb_log) if one_sided_lb_log is not None else None
        # Note: median of log-bootstrap, exponentiated, is the GEOMETRIC median
        # of PF resamples — biased low vs arithmetic mean PF (Jensen).
        # Reported for consistency with the log-space CI; not the same as a
        # naive median of PF resamples.
        boot_geo_median = math.exp(statistics.median(boot_log)) if boot_log else None
        boot_se_log = statistics.stdev(boot_log) if len(boot_log) >= 2 else None

        out['point_estimate'] = point
        out['ci_lo'] = lo
        out['ci_hi'] = hi
        out['one_sided_lb'] = one_sided_lb
        # Asymmetric arms for PF in original-PF space
        out['ci_arm_lower'] = (point - lo) if (point is not None and lo is not None) else None
        out['ci_arm_upper'] = (hi - point) if (point is not None and hi is not None) else None
        out['bootstrap_median'] = boot_geo_median  # geometric (see note above)
        out['bootstrap_stderr_log'] = boot_se_log  # SE in log-PF space, not direct PF
        out['n_bootstrap_pf_undefined'] = undefined
        out['n_needed_reason'] = 'wald-not-valid-for-pf'

        und_frac = undefined / bootstrap_iters if bootstrap_iters else 0
        if und_frac > PF_UNDEFINED_WARN_THRESHOLD:
            warnings.append(f'pf_undefined_resamples_{und_frac:.1%}')
        if point is None and (lo is not None or hi is not None):
            warnings.append('pf_point_estimate_undefined_but_ci_computed')

    else:
        raise ValueError(f'unknown metric: {metric}')

    out['status'] = sample_status
    out['warnings'] = warnings
    return out


def _format_human(result, symbol, since_spec):
    def fmt(x, n=4, signed=True):
        if x is None:
            return 'n/a'
        if isinstance(x, float):
            return f'{x:+.{n}f}' if signed else f'{x:.{n}f}'
        return str(x)

    metric = result['metric']
    lines = []
    lines.append(f"--- Bootstrap CI ({metric}) ---")
    lines.append(f"symbol         : {symbol or 'all'}")
    lines.append(f"window         : {since_spec or 'all-time'}")
    lines.append(f"trades total   : {result['n_trades_total']}")
    lines.append(f"trades used    : {result['n_trades_used']}"
                 f" (skipped {result['n_skipped_no_atr_or_entry']} for missing atr/entry)")
    lines.append(f"status         : {result['status']}")
    warnings = result.get('warnings') or []
    if warnings:
        lines.append(f"warnings       : {', '.join(warnings)}")
    if result['status'].startswith('insufficient'):
        return '\n'.join(lines)

    point = result['point_estimate']
    lo = result['ci_lo']
    hi = result['ci_hi']
    one_sided = result.get('one_sided_lb')
    arm_lo = result.get('ci_arm_lower')
    arm_hi = result.get('ci_arm_upper')
    median = result['bootstrap_median']
    conf = result.get('one_sided_confidence', 0.95)
    conf_pct = int(round(conf * 100))

    lines.append(f"point estimate : {fmt(point)}")
    if metric == 'expectancy_r':
        lines.append(f"bootstrap med  : {fmt(median)}")
        lines.append(f"{conf_pct}% CI        : [{fmt(lo)}, {fmt(hi)}]")
        if arm_lo is not None and arm_hi is not None:
            lines.append(f"CI arms        : -{fmt(arm_lo, signed=False)} / +{fmt(arm_hi, signed=False)}  (asymmetric if skewed)")
        lines.append(f"one-sided {conf_pct}%LB: {fmt(one_sided)}   <- gate decision quantity")
        sigma = result.get('sample_stdev_r')
        sigma_ci = result.get('sample_stdev_r_ci') or [None, None]
        lines.append(f"sample sigma(R): {fmt(sigma, signed=False)}  [sigma {conf_pct}% CI: {fmt(sigma_ci[0], signed=False)} .. {fmt(sigma_ci[1], signed=False)}]")
        n_total = result.get('n_needed_total')
        n_lo = result.get('n_needed_lo')
        n_hi = result.get('n_needed_hi')
        additional = result.get('recommended_additional_n')
        target = result.get('target_halfwidth_r')
        if n_total is not None:
            range_str = ''
            if n_lo is not None and n_hi is not None:
                range_str = f"  [range from sigma CI: {n_lo} .. {n_hi}]"
            lines.append(f"trades needed  : ~{n_total} total for +-{target}R half-width{range_str}")
            if additional == 0:
                lines.append("                 -> already at target")
            else:
                lines.append(f"                 -> {additional} more trades needed")
    else:
        lines.append(f"bootstrap median: {fmt(median, 3)}  (geometric — exp(median(log PF)); biased low vs arithmetic mean)")
        lines.append(f"{conf_pct}% CI (log-bs): [{fmt(lo, 3)}, {fmt(hi, 3)}]")
        if arm_lo is not None and arm_hi is not None:
            lines.append(f"CI arms        : -{fmt(arm_lo, 3, signed=False)} / +{fmt(arm_hi, 3, signed=False)}")
        lines.append(f"one-sided {conf_pct}%LB: {fmt(one_sided, 3)}")
        se_log = result.get('bootstrap_stderr_log')
        if se_log is not None:
            lines.append(f"SE log(PF)     : {fmt(se_log, 3, signed=False)}  (in log space)")
        und = result.get('n_bootstrap_pf_undefined', 0)
        if und:
            lines.append(f"PF undefined in {und}/{result['bootstrap_iters']} resamples (dropped from CI)")
        lines.append("note           : Wald N-estimate not valid for PF (skewed ratio); CI is on log(PF), exponentiated")
    return '\n'.join(lines)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--db', default=DEFAULT_DB)
    p.add_argument('--symbol', default='all', help="symbol or 'all' (default)")
    p.add_argument('--days', default='',
                   help="window like '24h', '7d', '30d', '4w'; default all-time")
    p.add_argument('--metric', choices=('expectancy_r', 'pf'), default='expectancy_r')
    p.add_argument('--bootstrap-iters', type=int, default=10000)
    p.add_argument('--confidence', type=float, default=0.95)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--sl-atr-mult', type=float, default=DEFAULT_SL_ATR_MULT)
    p.add_argument('--target-halfwidth', type=float,
                   default=DEFAULT_TARGET_HALFWIDTH_R,
                   help='target CI half-width in R for n_needed estimate')
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if not 0 < args.confidence < 1:
        print(f"error: --confidence must be in (0,1), got {args.confidence}", file=sys.stderr)
        return 2
    if args.bootstrap_iters < 1:
        print("error: --bootstrap-iters must be positive", file=sys.stderr)
        return 2
    if args.target_halfwidth <= 0:
        print("error: --target-halfwidth must be positive", file=sys.stderr)
        return 2

    since_ts, ok = _parse_since(args.days)
    if args.days and not ok:
        print(f"error: --days '{args.days}' not recognized (use '24h', '7d', '30d', '4w')",
              file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.Error as e:
        print(f"error: cannot open db at {args.db}: {e}", file=sys.stderr)
        return 1

    try:
        trades = _fetch_trades(conn, since_ts, args.symbol)
    except sqlite3.OperationalError as e:
        print(f"error: db query failed ({e}); is the analytics_trades table initialized?",
              file=sys.stderr)
        return 1
    finally:
        conn.close()

    result = compute(
        trades,
        metric=args.metric,
        sl_atr_mult=args.sl_atr_mult,
        bootstrap_iters=args.bootstrap_iters,
        confidence=args.confidence,
        seed=args.seed,
        target_halfwidth=args.target_halfwidth,
    )
    result['symbol'] = args.symbol
    result['window'] = args.days or 'all-time'

    print(_format_human(result, args.symbol, args.days), file=sys.stderr)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
