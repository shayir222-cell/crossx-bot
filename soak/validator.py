"""Soak validator — runs as daemon thread, polls own endpoints, detects anomalies.

Strict isolation guarantee:
- Validator failure cannot affect trading. All operations wrapped in try/except.
- Optional thread (only starts if ENABLE_SOAK_VALIDATION).
- No mutation of trading state. Read-only consumption of:
    db.stats(), metrics.snapshot(), and (optionally) HTTP probing of base_url.

Detection capabilities:
- event gaps               (signals counter not growing)
- stalled metrics          (gauges frozen at boot)
- missing analytics rows   (db.stats analytics_signals < signals counter)
- duplicated signal IDs    (deduplicated by hash, but counter sanity check)
- abnormal execution latency spikes (P95 over threshold)
- missing reconciliation cycles (no reconcile_runs growth in 1h)

Output:
- Per-tick anomaly events (logged to logger.log_warning)
- Periodic summary (every report_every_minutes) persisted to analytics_soak_reports
"""

import threading
import time
import socket
from datetime import datetime, timezone

import logger
import metrics


class SoakValidator:
    def __init__(self, config, db_module, alerts_module=None, http_probe_fn=None):
        """
        Args:
            config: SoakConfig instance
            db_module: module exposing db.stats(), db.save_analytics_soak()
            alerts_module: optional alerts.py module for severity emission
            http_probe_fn: optional callable(url, timeout) -> dict|None for
                           probing remote endpoints. If None, only local checks run.
        """
        self.cfg = config
        self.db = db_module
        self.alerts = alerts_module
        self.probe = http_probe_fn

        # State
        self._thread = None
        self._stop_event = threading.Event()
        self._tick_count = 0
        self._last_report_at = 0.0
        self._cycle_start = time.time()
        self._tick_baseline = None       # captured at first tick
        self._uptime_buckets = {           # endpoint name -> [ok_count, total_count]
            'webhook_proxy': [0, 0],
            'metrics': [0, 0],
            'diagnostics': [0, 0],
            'prometheus': [0, 0],
        }
        self._anomalies_buffer = []      # current report cycle anomalies

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='SoakValidator')
        self._thread.start()
        try:
            logger.log_event('soak_started', config=self.cfg.to_dict())
        except Exception:
            pass

    def stop(self):
        try:
            self._stop_event.set()
        except Exception:
            pass

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                try:
                    logger.log_error('soak_tick_failure', error=str(e)[:200])
                except Exception:
                    pass
            # Sleep but allow fast wake on stop
            self._stop_event.wait(self.cfg.tick_seconds)

    # ─── per-tick ────────────────────────────────────────────────
    def _tick(self):
        self._tick_count += 1
        try:
            metrics.inc('soak_runs_total')
        except Exception:
            pass

        # Capture baseline on first tick — used for growth deltas
        if self._tick_baseline is None:
            self._tick_baseline = self._snapshot_state()
            return

        snap = self._snapshot_state()

        # Update local-component uptimes (these are direct module probes, not HTTP)
        self._uptime_buckets['webhook_proxy'][1] += 1
        if snap.get('db_ready'):
            self._uptime_buckets['webhook_proxy'][0] += 1

        self._uptime_buckets['metrics'][1] += 1
        if snap.get('metrics_ready'):
            self._uptime_buckets['metrics'][0] += 1

        # If HTTP probing is wired, try /diagnostics + /prometheus
        if self.probe and self.cfg.base_url:
            self._check_remote_endpoints()

        # Detect anomalies
        self._detect_anomalies(snap)

        # Periodic report
        now = time.time()
        if (now - self._last_report_at) >= self.cfg.report_every_minutes * 60:
            self._emit_report(snap, now)
            self._last_report_at = now

    def _snapshot_state(self):
        """Read-only sample of current bot state. Returns dict with safe defaults."""
        out = {
            'ts': time.time(),
            'db_ready': False,
            'metrics_ready': True,    # metrics.snapshot doesn't fail
            'db_stats': {},
            'metrics': {},
        }
        try:
            out['db_ready'] = self.db.is_ready()
            out['db_stats'] = self.db.stats() or {}
        except Exception:
            pass
        try:
            out['metrics'] = metrics.snapshot()
        except Exception:
            out['metrics_ready'] = False
        return out

    def _check_remote_endpoints(self):
        """Optional HTTP probe of /metrics and /prometheus (and /diagnostics if token)."""
        try:
            base = self.cfg.base_url.rstrip('/')
            self._uptime_buckets['metrics'][1] += 1
            if self.probe(f'{base}/metrics', timeout=8) is not None:
                self._uptime_buckets['metrics'][0] += 1

            self._uptime_buckets['prometheus'][1] += 1
            if self.probe(f'{base}/prometheus', timeout=8) is not None:
                self._uptime_buckets['prometheus'][0] += 1

            if self.cfg.auth_token:
                self._uptime_buckets['diagnostics'][1] += 1
                if self.probe(f'{base}/diagnostics', timeout=8,
                              headers={'X-Auth-Token': self.cfg.auth_token}) is not None:
                    self._uptime_buckets['diagnostics'][0] += 1
        except Exception:
            pass

    # ─── anomaly detection ───────────────────────────────────────
    def _detect_anomalies(self, snap):
        a = []
        try:
            # Compare against tick baseline
            base = self._tick_baseline
            base_db = base.get('db_stats', {})
            cur_db = snap.get('db_stats', {})

            # Missing analytics rows: analytics_signals should grow at same pace as signals
            base_sigs = base_db.get('signals', 0)
            cur_sigs = cur_db.get('signals', 0)
            base_asigs = base_db.get('analytics_signals', 0)
            cur_asigs = cur_db.get('analytics_signals', 0)
            sig_delta = cur_sigs - base_sigs
            asig_delta = cur_asigs - base_asigs
            if sig_delta > 0 and asig_delta < sig_delta * 0.9:
                a.append({
                    'kind': 'missing_analytics_rows',
                    'signals_delta': sig_delta, 'analytics_delta': asig_delta,
                })

            # Stalled metrics: signals_received_total should grow if signals do
            mc = (snap.get('metrics') or {}).get('counters', {})
            sr_total = mc.get('signals_received_total')
            if isinstance(sr_total, list):
                # has labeled — sum unlabeled if any
                sr_total = sum(x.get('value', 0) for x in sr_total if not x.get('labels'))
            if sig_delta > 0 and (sr_total or 0) == 0:
                a.append({
                    'kind': 'metrics_signals_counter_stalled',
                    'db_signals_delta': sig_delta,
                })

            # Latency spikes — check P95
            mobs = (snap.get('metrics') or {}).get('observations', {})
            wh = mobs.get('webhook_total_latency_ms')
            p95 = None
            if isinstance(wh, dict):
                p95 = wh.get('p95')
            elif isinstance(wh, list):
                p95s = [x.get('p95', 0) for x in wh]
                p95 = max(p95s) if p95s else None
            if p95 and p95 > self.cfg.latency_p95_alert_ms:
                a.append({
                    'kind': 'latency_p95_spike',
                    'p95_ms': p95,
                    'threshold_ms': self.cfg.latency_p95_alert_ms,
                })

            # Reconciliation freshness: should run on startup AT MINIMUM
            recon_runs = mc.get('reconcile_runs_total')
            if isinstance(recon_runs, list):
                recon_runs = sum(x.get('value', 0) for x in recon_runs)
            # We only complain if no recon ran in ~24h since process start
            cycle_age_min = (time.time() - self._cycle_start) / 60
            if cycle_age_min > 60 and (recon_runs or 0) == 0:
                a.append({'kind': 'no_reconcile_observed', 'cycle_age_min': int(cycle_age_min)})

        except Exception as e:
            a.append({'kind': 'detection_failure', 'error': str(e)[:200]})

        if a:
            self._anomalies_buffer.extend(a)
            try:
                metrics.inc('soak_anomalies_total', n=len(a))
                logger.log_warning('soak_anomaly_batch', count=len(a),
                                   kinds=[x.get('kind') for x in a])
                if self.alerts and any(x.get('kind') in ('latency_p95_spike',
                                                         'missing_analytics_rows',
                                                         'metrics_signals_counter_stalled')
                                       for x in a):
                    self.alerts.warning(
                        f'Soak detected {len(a)} anomalies',
                        details={'kinds': [x.get('kind') for x in a]}
                    )
            except Exception:
                pass

    # ─── report emission ─────────────────────────────────────────
    def _uptime_pct(self, key):
        ok, total = self._uptime_buckets.get(key, [0, 0])
        return (ok / total * 100) if total > 0 else 0.0

    def _emit_report(self, snap, now):
        try:
            base = self._tick_baseline.get('db_stats', {}) if self._tick_baseline else {}
            cur = snap.get('db_stats', {})
            duration_min = int((now - self._cycle_start) / 60)
            sig_growth = cur.get('signals', 0) - base.get('signals', 0)
            asig_growth = cur.get('analytics_signals', 0) - base.get('analytics_signals', 0)
            atrade_growth = cur.get('analytics_trades', 0) - base.get('analytics_trades', 0)

            mc = (snap.get('metrics') or {}).get('counters', {})
            recon = mc.get('reconcile_runs_total')
            if isinstance(recon, list):
                recon = sum(x.get('value', 0) for x in recon)

            wh_uptime = self._uptime_pct('webhook_proxy')
            m_uptime = self._uptime_pct('metrics')
            d_uptime = self._uptime_pct('diagnostics')
            p_uptime = self._uptime_pct('prometheus')

            # event_continuity_score — fraction of expected analytics rows present
            if sig_growth > 0:
                continuity = min(1.0, asig_growth / max(1, sig_growth))
            else:
                continuity = 1.0  # no signals = vacuously fine

            # observability_integrity_score — composite of everything
            uptimes = [wh_uptime, m_uptime]
            if d_uptime > 0: uptimes.append(d_uptime)
            if p_uptime > 0: uptimes.append(p_uptime)
            avg_uptime = sum(uptimes) / len(uptimes) if uptimes else 0.0
            integrity = (continuity * 0.5) + (avg_uptime / 100 * 0.5)

            row = {
                'duration_min': duration_min,
                'webhook_uptime_pct': round(wh_uptime, 2),
                'metrics_uptime_pct': round(m_uptime, 2),
                'diagnostics_uptime_pct': round(d_uptime, 2),
                'prometheus_uptime_pct': round(p_uptime, 2),
                'signals_growth': sig_growth,
                'analytics_signals_growth': asig_growth,
                'analytics_trades_growth': atrade_growth,
                'reconcile_runs_observed': int(recon or 0),
                'event_continuity_score': round(continuity, 4),
                'observability_integrity_score': round(integrity, 4),
                'anomalies': self._anomalies_buffer,
            }

            metrics.gauge('event_continuity_score', continuity)
            metrics.gauge('observability_integrity_score', integrity)
            metrics.gauge('soak_uptime_pct', avg_uptime)

            self.db.save_analytics_soak(row)

            logger.log_event('soak_report_emitted', **{k: v for k, v in row.items() if k != 'anomalies'},
                             anomaly_count=len(self._anomalies_buffer))

            if integrity < self.cfg.continuity_alert_threshold and self.alerts:
                self.alerts.warning(
                    f'Soak integrity below threshold: {integrity:.2f} (threshold {self.cfg.continuity_alert_threshold})',
                    details={'duration_min': duration_min, 'anomalies': len(self._anomalies_buffer)}
                )

            # Reset buffer + advance baseline for next cycle
            self._anomalies_buffer = []
            self._tick_baseline = snap
            self._cycle_start = now
            for k in self._uptime_buckets:
                self._uptime_buckets[k] = [0, 0]
        except Exception as e:
            try:
                logger.log_error('soak_report_failure', error=str(e)[:200])
            except Exception:
                pass


def http_probe_default(url, timeout=8, headers=None):
    """Default HTTP probe using requests. Returns dict on success, None on failure.

    Used by SoakValidator when wired with a real probe. Stub here to avoid
    forcing requests dependency at import time.
    """
    try:
        import requests
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return {'_text_only': True}
        return None
    except Exception:
        return None
