"""In-memory counters/gauges for observability. Thread-safe, fail-safe.

This module keeps process-local Prometheus-style metrics. They are NOT persisted
across restarts — by design. Long-term aggregates live in SQLite (see db.py).

Public API:
    inc(name, n=1)     # increment counter
    gauge(name, val)   # set gauge to value
    add_obs(name, val) # append to bounded observation list (for avg/quantile)
    snapshot()         # full snapshot {counters, gauges, observations}
    reset()            # for tests only

Naming convention (Prometheus-friendly):
    <subsystem>_<event>_total      counters
    <subsystem>_<thing>_pct/_ms    gauges

If any function fails internally it returns silently — observability must
never break trading.
"""

import threading
from collections import deque

_lock = threading.Lock()
_counters = {}
_gauges = {}
_observations = {}     # name -> bounded deque of values
_OBS_MAX = 500         # per-name retention


def inc(name, n=1):
    try:
        with _lock:
            _counters[name] = _counters.get(name, 0) + int(n)
    except Exception:
        pass


def gauge(name, value):
    try:
        v = float(value)
        with _lock:
            _gauges[name] = v
    except Exception:
        pass


def add_obs(name, value):
    """Append observation to bounded buffer for avg/percentile calculation."""
    try:
        v = float(value)
        with _lock:
            buf = _observations.get(name)
            if buf is None:
                buf = deque(maxlen=_OBS_MAX)
                _observations[name] = buf
            buf.append(v)
    except Exception:
        pass


def avg(name):
    try:
        with _lock:
            buf = _observations.get(name)
            if not buf:
                return 0.0
            return sum(buf) / len(buf)
    except Exception:
        return 0.0


def snapshot():
    try:
        with _lock:
            return {
                'counters': dict(_counters),
                'gauges': dict(_gauges),
                'observations': {
                    k: {
                        'count': len(v),
                        'avg': (sum(v) / len(v)) if v else 0.0,
                        'min': (min(v) if v else 0.0),
                        'max': (max(v) if v else 0.0),
                    }
                    for k, v in _observations.items()
                },
            }
    except Exception:
        return {'counters': {}, 'gauges': {}, 'observations': {}}


def list_counters():
    try:
        with _lock:
            return dict(_counters)
    except Exception:
        return {}


def list_gauges():
    try:
        with _lock:
            return dict(_gauges)
    except Exception:
        return {}


def reset():
    """For tests only — wipes all metrics."""
    try:
        with _lock:
            _counters.clear()
            _gauges.clear()
            _observations.clear()
    except Exception:
        pass


# Pre-register canonical metric names so /metrics endpoint shows zeros
# even before any event fires (better than missing keys).
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
)
_CANONICAL_GAUGES = (
    'current_drawdown_pct', 'current_win_streak', 'current_loss_streak',
    'active_positions', 'daily_pnl_pct', 'daily_peak_pnl_pct',
    'avg_signal_score', 'avg_execution_latency_ms',
    'session_pnl_asian', 'session_pnl_london', 'session_pnl_overlap',
    'session_pnl_ny',
)


def init_canonical():
    """Pre-populate canonical names with zero. Optional but recommended."""
    try:
        with _lock:
            for c in _CANONICAL_COUNTERS:
                _counters.setdefault(c, 0)
            for g in _CANONICAL_GAUGES:
                _gauges.setdefault(g, 0.0)
    except Exception:
        pass
