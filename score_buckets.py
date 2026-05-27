"""Shared score-bucket monotonicity computation.

Used by robustness.py (Q2 gate) and monotonicity.py (standalone analyzer)
so the two cannot drift. The strategy v2 score is 0-100 with a gate at 92;
above the gate it should monotonically predict r_net but empirical data
(see project_score_model_flat_above_92.md) shows it doesn't always.

Constants:
    SCORE_BUCKETS, SCORE_MIDPOINTS
    Q2_MIN_NON_EMPTY_BUCKETS, Q2_FAIL_RHO, Q2_PASS_RHO

API:
    compute_monotonicity(trades) -> dict
        trades: iterable of {score, r_net, ...} dicts
        returns: {status, spearman_rho, reason, caveat, buckets}
        status: PASS (rho>=0.30) / GRAY ([0, 0.30)) / FAIL (<0) / INCONCLUSIVE
        Low-power soften: when only 3 buckets are non-empty AND
            |rho| < LOW_POWER_STRICT_RHO (default 0.70), FAIL/PASS
            are downgraded to GRAY since Spearman on 3 points is a
            discrete-noise statistic at non-extreme values.
"""
import statistics


SCORE_BUCKETS = [(92, 94), (94, 96), (96, 98), (98, 101)]
SCORE_MIDPOINTS = [(lo + hi - 1) / 2.0 for lo, hi in SCORE_BUCKETS]

Q2_MIN_NON_EMPTY_BUCKETS = 3
Q2_FAIL_RHO = 0.0     # rho < 0 = anti-predictive => FAIL
Q2_PASS_RHO = 0.30    # rho >= 0.30 => PASS; [0, 0.30) => GRAY

# When only 3 buckets are non-empty, Spearman on 3 points yields a discrete
# set of values (-1, -0.5, +0.5, +1 after tie resolution). The weak |rho|=0.5
# verdict at 3 buckets is too noisy to drive a FAIL/PASS — soften to GRAY
# unless the signal is strong (|rho| >= LOW_POWER_STRICT_RHO).
LOW_POWER_STRICT_RHO = 0.70


def compute_monotonicity(trades):
    """Bucket trades by score, compute Spearman rho between bucket midpoint
    and bucket-mean r_net, classify.

    trades: iterable of dicts with 'score' (int/float) and 'r_net' (float).
    """
    if not trades:
        return {
            'status': 'INCONCLUSIVE',
            'reason': 'no trades',
            'spearman_rho': None,
            'caveat': None,
            'buckets': [
                {'label': f'[{lo},{hi})', 'n': 0, 'mean_r_net': None}
                for lo, hi in SCORE_BUCKETS
            ],
        }

    bucket_means = []
    bucket_counts = []
    bucket_labels = []
    for lo, hi in SCORE_BUCKETS:
        in_bucket = [t for t in trades if lo <= t['score'] < hi]
        if in_bucket:
            bucket_means.append(
                sum(t['r_net'] for t in in_bucket) / len(in_bucket))
            bucket_counts.append(len(in_bucket))
        else:
            bucket_means.append(None)
            bucket_counts.append(0)
        bucket_labels.append(f'[{lo},{hi})')

    bucket_diag = [
        {'label': bucket_labels[i], 'n': bucket_counts[i],
         'mean_r_net': bucket_means[i]}
        for i in range(len(SCORE_BUCKETS))
    ]

    non_empty_idx = [i for i, m in enumerate(bucket_means) if m is not None]
    if len(non_empty_idx) < Q2_MIN_NON_EMPTY_BUCKETS:
        return {
            'status': 'INCONCLUSIVE',
            'reason': f'only {len(non_empty_idx)} non-empty score buckets, '
                      f'need >={Q2_MIN_NON_EMPTY_BUCKETS}',
            'spearman_rho': None,
            'caveat': None,
            'buckets': bucket_diag,
        }

    midpoints = [SCORE_MIDPOINTS[i] for i in non_empty_idx]
    means = [bucket_means[i] for i in non_empty_idx]
    try:
        rho = statistics.correlation(midpoints, means, method='ranked')
    except (statistics.StatisticsError, AttributeError) as e:
        return {
            'status': 'INCONCLUSIVE',
            'reason': f'spearman computation failed: {e}',
            'spearman_rho': None,
            'caveat': None,
            'buckets': bucket_diag,
        }

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
    # Low-power soften: at the 3-bucket boundary, only extreme |rho| values
    # warrant a confident FAIL/PASS verdict. Soften the middle to GRAY.
    if len(non_empty_idx) == Q2_MIN_NON_EMPTY_BUCKETS:
        caveat = f'low statistical power (only {Q2_MIN_NON_EMPTY_BUCKETS} buckets)'
        if abs(rho) < LOW_POWER_STRICT_RHO and status in ('FAIL', 'PASS'):
            status = 'GRAY'
            narrative += ' [softened to GRAY: 3-bucket low power]'

    return {
        'status': status,
        'spearman_rho': rho,
        'reason': f'spearman rho={rho:+.3f} ({narrative}; '
                  f'FAIL<{Q2_FAIL_RHO}, GRAY [{Q2_FAIL_RHO},{Q2_PASS_RHO}), PASS>={Q2_PASS_RHO})',
        'caveat': caveat,
        'buckets': bucket_diag,
    }
