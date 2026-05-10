"""In-memory counters/gauges/observations for observability.

P1 (v3.7) — Adds Prometheus-style label support with strict cardinality
control. Backward-compatible with all v3.6 callers:
    metrics.inc('foo')               # unlabeled, as before
    metrics.gauge('bar', 5.0)        # unlabeled, as before
    metrics.add_obs('lat', 120)      # unlabeled, as before
    metrics.inc('foo', symbol='BTC') # NEW: labeled
    metrics.gauge('pnl', 1.5, symbol='ETH', session='NY')

Storage:
    _counters[name]      -> dict[labelset_tuple -> int]
    _gauges[name]        -> dict[labelset_tuple -> float]
    _observations[name]  -> dict[labelset_tuple -> deque(maxlen=500)]

labelset_tuple is `tuple(sorted(labels.items()))` for determinism, or `()`
for unlabeled. Mixing labeled+unlabeled on the same name is allowed but
discouraged (Prometheus emits both).

Cardinality protection:
    Each metric name has a per-process cap of MAX_LABELSETS_PER_METRIC.
    Operations beyond the cap are silently dropped and counted in
    `_dropped_cardinality` (visible via snapshot()).
"""

import threading
from collections import deque

_lock = threading.Lock()
_counters = {}        # name -> dict[labelset_tuple -> int]
_gauges = {}          # name -> dict[labelset_tuple -> float]
_observations = {}    # name -> dict[labelset_tuple -> deque]
_dropped_cardinality = {}  # name -> count of dropped writes

_OBS_MAX = 500
MAX_LABELSETS_PER_METRIC = 64    # cardinality safety
_RESERVED = {'__name__', 'job', 'instance'}


def _normalize_labels(labels):
    """Convert labels dict to a deterministic, hashable tuple."""
    if not labels:
        return ()
    try:
        items = []
        for k, v in labels.items():
            if not k or k in _RESERVED:
                continue
            ks = str(k)
            vs = str(v) if v is not None else ''
            # Bound label value length
            if len(vs) > 64:
                vs = vs[:64]
            items.append((ks, vs))
        items.sort()
        return tuple(items)
    except Exception:
        return ()


def _check_cardinality(name, labelset, store):
    """Returns True if this labelset is allowed; False if at cap."""
    table = store.get(name)
    if table is None or labelset in table:
        return True
    if len(table) >= MAX_LABELSETS_PER_METRIC:
        try:
            _dropped_cardinality[name] = _dropped_cardinality.get(name, 0) + 1
        except Exception:
            pass
        return False
    return True


def inc(name, n=1, **labels):
    try:
        ls = _normalize_labels(labels)
        with _lock:
            if not _check_cardinality(name, ls, _counters):
                return
            table = _counters.setdefault(name, {})
            table[ls] = table.get(ls, 0) + int(n)
    except Exception:
        pass


def gauge(name, value, **labels):
    try:
        v = float(value)
        ls = _normalize_labels(labels)
        with _lock:
            if not _check_cardinality(name, ls, _gauges):
                return
            table = _gauges.setdefault(name, {})
            table[ls] = v
    except Exception:
        pass


def add_obs(name, value, **labels):
    try:
        v = float(value)
        ls = _normalize_labels(labels)
        with _lock:
            if not _check_cardinality(name, ls, _observations):
                return
            table = _observations.setdefault(name, {})
            buf = table.get(ls)
            if buf is None:
                buf = deque(maxlen=_OBS_MAX)
                table[ls] = buf
            buf.append(v)
    except Exception:
        pass


def avg(name, **labels):
    try:
        ls = _normalize_labels(labels)
        with _lock:
            buf = _observations.get(name, {}).get(ls)
            if not buf:
                return 0.0
            return sum(buf) / len(buf)
    except Exception:
        return 0.0


def percentile(name, p, **labels):
    """Compute p-th percentile (0-100) over observation buffer. O(N log N)."""
    try:
        ls = _normalize_labels(labels)
        with _lock:
            buf = _observations.get(name, {}).get(ls)
            if not buf:
                return 0.0
            arr = sorted(buf)
        if not arr:
            return 0.0
        k = max(0, min(len(arr) - 1, int(round((p / 100.0) * (len(arr) - 1)))))
        return float(arr[k])
    except Exception:
        return 0.0


def _flatten(table, scalar_only=False):
    """Convert {labelset_tuple: value} into:
       - scalar `value` if only () labelset present (backward compat)
       - list of {labels: dict, value} otherwise
    """
    if not table:
        return 0 if scalar_only else None
    if len(table) == 1 and () in table:
        return table[()]
    return [
        {'labels': dict(ls), 'value': v}
        for ls, v in sorted(table.items(), key=lambda kv: str(kv[0]))
    ]


def snapshot():
    """Combined snapshot. Backward-compatible scalar form for unlabeled metrics."""
    try:
        with _lock:
            counters_out = {}
            for name, table in _counters.items():
                counters_out[name] = _flatten(table)

            gauges_out = {}
            for name, table in _gauges.items():
                gauges_out[name] = _flatten(table)

            obs_out = {}
            for name, table in _observations.items():
                if len(table) == 1 and () in table:
                    buf = table[()]
                    obs_out[name] = {
                        'count': len(buf),
                        'avg': (sum(buf) / len(buf)) if buf else 0.0,
                        'min': min(buf) if buf else 0.0,
                        'max': max(buf) if buf else 0.0,
                        'p50': _calc_p(buf, 50),
                        'p95': _calc_p(buf, 95),
                        'p99': _calc_p(buf, 99),
                    }
                else:
                    obs_out[name] = []
                    for ls, buf in table.items():
                        obs_out[name].append({
                            'labels': dict(ls),
                            'count': len(buf),
                            'avg': (sum(buf) / len(buf)) if buf else 0.0,
                            'min': min(buf) if buf else 0.0,
                            'max': max(buf) if buf else 0.0,
                            'p50': _calc_p(buf, 50),
                            'p95': _calc_p(buf, 95),
                            'p99': _calc_p(buf, 99),
                        })

            return {
                'counters': counters_out,
                'gauges': gauges_out,
                'observations': obs_out,
                'cardinality_dropped': dict(_dropped_cardinality),
            }
    except Exception:
        return {'counters': {}, 'gauges': {}, 'observations': {}, 'cardinality_dropped': {}}


def _calc_p(buf, p):
    if not buf:
        return 0.0
    arr = sorted(buf)
    k = max(0, min(len(arr) - 1, int(round((p / 100.0) * (len(arr) - 1)))))
    return float(arr[k])


def list_counters():
    """Returns counters in (name, labelset_tuple, value) form for Prometheus export."""
    try:
        with _lock:
            out = []
            for name, table in _counters.items():
                for ls, v in table.items():
                    out.append((name, ls, v))
            return out
    except Exception:
        return []


def list_gauges():
    try:
        with _lock:
            out = []
            for name, table in _gauges.items():
                for ls, v in table.items():
                    out.append((name, ls, v))
            return out
    except Exception:
        return []


def list_observations():
    """Returns (name, labelset_tuple, stats_dict) for Prometheus export."""
    try:
        with _lock:
            out = []
            for name, table in _observations.items():
                for ls, buf in table.items():
                    out.append((name, ls, {
                        'count': len(buf),
                        'avg': (sum(buf) / len(buf)) if buf else 0.0,
                        'min': min(buf) if buf else 0.0,
                        'max': max(buf) if buf else 0.0,
                        'p50': _calc_p(buf, 50),
                        'p95': _calc_p(buf, 95),
                        'p99': _calc_p(buf, 99),
                    }))
            return out
    except Exception:
        return []


def reset():
    """For tests only — wipes all metrics."""
    try:
        with _lock:
            _counters.clear()
            _gauges.clear()
            _observations.clear()
            _dropped_cardinality.clear()
    except Exception:
        pass


# ─── Canonical metric names (pre-populate to zero) ────────────────────
_CANONICAL_COUNTERS = (
    'signals_received_total', 'signals_rejected_total', 'signals_taken_total',
    'duplicate_signals_blocked_total', 'cooldowns_triggered_total',
    'orders_submitted_total', 'orders_failed_total',
    'trades_opened_total', 'trades_closed_total',
    'trades_won_total', 'trades_lost_total',
    'tp_hits_total', 'sl_hits_total', 'trail_stops_total',
    'max_giveback_total', 'time_stops_total', 'manual_closes_total',
    'reconcile_runs_total', 'reconcile_warnings_total',
    'reconcile_failures_total', 'unmanaged_positions_total',
    'risk_halts_total', 'daily_dd_halts_total',
    'close_fail_total', 'api_errors_total',
    # P1 additions
    'webhook_requests_total', 'webhook_latency_anomalies_total',
    'soak_runs_total', 'soak_anomalies_total',
    'fills_received_total', 'fills_reconciled_total', 'fills_orphaned_total',
)
_CANONICAL_GAUGES = (
    'current_drawdown_pct', 'current_win_streak', 'current_loss_streak',
    'active_positions', 'daily_pnl_pct', 'daily_peak_pnl_pct',
    'avg_signal_score', 'avg_execution_latency_ms',
    'session_pnl_asian', 'session_pnl_london', 'session_pnl_overlap', 'session_pnl_ny',
    # P1 additions
    'observability_integrity_score', 'event_continuity_score',
    'reliability_score', 'execution_consistency_score',
    'reconcile_accuracy_score', 'operational_readiness_score',
    'soak_uptime_pct',
)


def init_canonical():
    try:
        with _lock:
            for c in _CANONICAL_COUNTERS:
                _counters.setdefault(c, {}).setdefault((), 0)
            for g in _CANONICAL_GAUGES:
                _gauges.setdefault(g, {}).setdefault((), 0.0)
    except Exception:
        pass
