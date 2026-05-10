"""Unit tests for stability_audit.py — score range, finding propagation."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics as M
import db
import stability_audit


class TestStabilityAudit(unittest.TestCase):
    def setUp(self):
        self.tmp_db = os.path.join(tempfile.gettempdir(),
                                    f'crossx_audit_{os.getpid()}_{id(self)}.db')
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        db._conn = None
        db.init_db(self.tmp_db)
        M.reset(); M.init_canonical()

    def tearDown(self):
        try:
            if db._conn:
                db._conn.close()
                db._conn = None
        except Exception:
            pass

    def test_clean_state_high_scores(self):
        result = stability_audit.run_audit(M, db)
        self.assertGreaterEqual(result['scores']['operational_readiness'], 90)

    def test_risk_halt_penalizes(self):
        M.inc('risk_halts_total')
        result = stability_audit.run_audit(M, db)
        self.assertLess(result['scores']['operational_readiness'], 90)
        self.assertTrue(any('risk_halt' in (f.get('msg') or '') for f in result['findings']))

    def test_close_failures_penalize(self):
        M.inc('close_fail_total', 5)
        result = stability_audit.run_audit(M, db)
        self.assertLess(result['scores']['operational_readiness'], 95)

    def test_unmanaged_position_critical(self):
        M.inc('unmanaged_positions_total')
        result = stability_audit.run_audit(M, db)
        # Penalty should hit 2 components: operational + reconciliation
        self.assertLess(result['scores']['operational_readiness'], 100)
        self.assertLess(result['scores']['reconciliation_accuracy'], 100)

    def test_order_failure_rate(self):
        M.inc('orders_submitted_total', 100)
        M.inc('orders_failed_total', 10)
        result = stability_audit.run_audit(M, db)
        self.assertLess(result['scores']['execution_consistency'], 90)

    def test_overall_in_range(self):
        result = stability_audit.run_audit(M, db)
        self.assertGreaterEqual(result['overall'], 0)
        self.assertLessEqual(result['overall'], 100)

    def test_recommendations_present(self):
        result = stability_audit.run_audit(M, db)
        self.assertIsInstance(result['recommendations'], list)
        self.assertGreater(len(result['recommendations']), 0)

    def test_markdown_render(self):
        result = stability_audit.run_audit(M, db)
        md = stability_audit.render_markdown(result)
        self.assertIn('Overall score', md)
        for k in result['scores']:
            self.assertIn(k.replace('_', ' ').title(), md)

    def test_audit_survives_metrics_failure(self):
        class BrokenMetrics:
            def snapshot(self):
                raise RuntimeError('metrics down')
            def gauge(self, *a, **kw):
                pass
        result = stability_audit.run_audit(BrokenMetrics(), db)
        # Should still return a result, not crash
        self.assertIn('scores', result)


if __name__ == '__main__':
    unittest.main()
