"""Unit tests for soak.validator — anomaly detection + report emission."""
import os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics as M
import db
from soak.config import SoakConfig
from soak.validator import SoakValidator
from soak.report import build_report, summarize_recent


class TestSoakValidator(unittest.TestCase):
    def setUp(self):
        self.tmp_db = os.path.join(tempfile.gettempdir(),
                                    f'crossx_soak_{os.getpid()}_{id(self)}.db')
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        db._conn = None
        db.init_db(self.tmp_db)
        M.reset(); M.init_canonical()
        # No real HTTP probes / no alerts module
        self.cfg = SoakConfig()
        self.v = SoakValidator(self.cfg, db_module=db, alerts_module=None,
                                http_probe_fn=None)

    def tearDown(self):
        try:
            if db._conn:
                db._conn.close()
                db._conn = None
        except Exception:
            pass

    def test_first_tick_captures_baseline(self):
        self.v._tick()
        self.assertIsNotNone(self.v._tick_baseline)

    def test_missing_analytics_anomaly(self):
        # Suppress auto-report during test (would clear buffer)
        self.v._last_report_at = time.time() + 100000
        self.v._tick_baseline = {'db_stats': {'signals': 0, 'analytics_signals': 0,
                                                'analytics_trades': 0}, 'metrics': {}}
        real = db.stats
        db.stats = lambda: {'signals': 100, 'analytics_signals': 5,
                             'analytics_trades': 0,
                             'analytics_soak_reports': 0}
        try:
            self.v._tick()
        finally:
            db.stats = real
        self.assertTrue(any(a.get('kind') == 'missing_analytics_rows'
                            for a in self.v._anomalies_buffer))

    def test_latency_p95_anomaly(self):
        # Suppress auto-report during test (would clear buffer)
        self.v._last_report_at = time.time() + 100000
        for v in [50, 100, 5000, 7000, 9000]:
            M.add_obs('webhook_total_latency_ms', v)
        self.v._tick_baseline = self.v._snapshot_state()
        self.v._anomalies_buffer = []
        self.v._tick()
        self.assertTrue(any(a.get('kind') == 'latency_p95_spike'
                            for a in self.v._anomalies_buffer))

    def test_report_persists_to_db(self):
        self.v._anomalies_buffer = [{'kind': 'test'}]
        self.v._tick_baseline = {
            'db_stats': {'signals': 100, 'analytics_signals': 100,
                          'analytics_trades': 5},
            'metrics': {}
        }
        self.v._cycle_start = time.time() - 600
        snap = self.v._snapshot_state()
        self.v._emit_report(snap, time.time())
        cur = db._conn.execute('SELECT COUNT(*) FROM analytics_soak_reports')
        self.assertGreaterEqual(cur.fetchone()[0], 1)

    def test_validator_survives_db_failure(self):
        real = db.stats
        db.stats = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            self.v._tick()  # must not raise
        finally:
            db.stats = real

    def test_build_report_renders_no_crash(self):
        md = build_report({
            'duration_min': 60, 'webhook_uptime_pct': 99.5,
            'metrics_uptime_pct': 100, 'diagnostics_uptime_pct': 99,
            'prometheus_uptime_pct': 100, 'signals_growth': 30,
            'analytics_signals_growth': 30, 'analytics_trades_growth': 1,
            'reconcile_runs_observed': 1,
            'event_continuity_score': 1.0,
            'observability_integrity_score': 0.99,
            'anomalies': [],
        })
        self.assertIn('Soak Report', md)

    def test_summarize_recent(self):
        s = summarize_recent([
            {'webhook_uptime_pct': 100, 'event_continuity_score': 1.0,
             'observability_integrity_score': 0.99, 'anomalies': []},
            {'webhook_uptime_pct': 90, 'event_continuity_score': 0.9,
             'observability_integrity_score': 0.92,
             'anomalies': [{'kind': 'x'}]},
        ])
        self.assertEqual(s['count'], 2)
        self.assertEqual(s['total_anomalies'], 1)
        self.assertEqual(s['avg_uptime_pct'], 95.0)

    def test_config_clamps(self):
        os.environ['SOAK_TICK_SECONDS'] = '5'
        cfg = SoakConfig()
        self.assertEqual(cfg.tick_seconds, 10)  # clamped to min
        os.environ['SOAK_TICK_SECONDS'] = '99999'
        cfg = SoakConfig()
        self.assertEqual(cfg.tick_seconds, 600)  # clamped to max


if __name__ == '__main__':
    unittest.main()
