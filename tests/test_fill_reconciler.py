"""Unit tests for fill_reconciler.py — slippage, partials, orphans, isolation."""
import os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import metrics as M
from fill_reconciler import FillReconciler


class TestFillReconciler(unittest.TestCase):
    def setUp(self):
        self.tmp_db = os.path.join(tempfile.gettempdir(), f'crossx_test_{os.getpid()}_{id(self)}.db')
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        # Re-init clean DB for each test
        db._conn = None
        db.init_db(self.tmp_db)
        M.reset()
        self.rec = FillReconciler(db_module=db, metrics_module=M)

    def tearDown(self):
        try:
            if db._conn:
                db._conn.close()
                db._conn = None
        except Exception:
            pass

    def test_long_entry_slippage_positive_when_filled_higher(self):
        self.rec.register_pending_order('o1', 'BTCUSDT', 'long',
                                         expected_price=50000, ts_submitted=time.time())
        self.rec.process_fill({'order_id': 'o1', 'symbol': 'BTCUSDT', 'side': 'long',
                                'fill_price': 50100, 'size': 0.001, 'ts_filled': time.time()})
        cur = db._conn.execute('SELECT slippage_pct FROM analytics_fills WHERE order_id="o1"')
        slip = cur.fetchone()[0]
        self.assertGreater(slip, 0)

    def test_short_entry_slippage_positive_when_filled_lower(self):
        self.rec.register_pending_order('o2', 'ETH', 'short',
                                         expected_price=3000, ts_submitted=time.time())
        self.rec.process_fill({'order_id': 'o2', 'symbol': 'ETH', 'side': 'short',
                                'fill_price': 2990, 'size': 0.01, 'ts_filled': time.time()})
        cur = db._conn.execute('SELECT slippage_pct FROM analytics_fills WHERE order_id="o2"')
        slip = cur.fetchone()[0]
        self.assertGreater(slip, 0)

    def test_orphan_fill(self):
        self.rec.process_fill({'order_id': 'unknown', 'symbol': 'X', 'side': 'long',
                                'fill_price': 1.0, 'size': 1.0, 'ts_filled': time.time()})
        s = M.snapshot()
        self.assertEqual(s['counters'].get('fills_orphaned_total'), 1)

    def test_partial_fills_each_persisted(self):
        self.rec.register_pending_order('o3', 'XRP', 'long', 2.30, ts_submitted=time.time())
        self.rec.process_fill({'order_id': 'o3', 'symbol': 'XRP', 'side': 'long',
                                'fill_price': 2.31, 'size': 50, 'ts_filled': time.time()})
        self.rec.process_fill({'order_id': 'o3', 'symbol': 'XRP', 'side': 'long',
                                'fill_price': 2.32, 'size': 50, 'ts_filled': time.time()})
        cur = db._conn.execute('SELECT COUNT(*) FROM analytics_fills WHERE order_id="o3"')
        self.assertEqual(cur.fetchone()[0], 2)

    def test_ttl_eviction(self):
        rec = FillReconciler(db_module=db, metrics_module=M, pending_ttl_seconds=1)
        rec.register_pending_order('old', 'X', 'long', 1, ts_submitted=time.time() - 10)
        rec.register_pending_order('new', 'X', 'long', 1, ts_submitted=time.time())
        self.assertEqual(rec.pending_count(), 1)

    def test_db_failure_does_not_propagate(self):
        class BrokenDB:
            def save_analytics_fill(self, row):
                raise RuntimeError('DB outage')
        rec = FillReconciler(db_module=BrokenDB(), metrics_module=M)
        rec.register_pending_order('x', 'BTC', 'long', 50000)
        # Must not raise
        rec.process_fill({'order_id': 'x', 'symbol': 'BTC', 'side': 'long',
                           'fill_price': 50100, 'size': 0.001, 'ts_filled': time.time()})

    def test_bad_fill_data_routes_to_orphan(self):
        self.rec.register_pending_order('bad', 'BTC', 'long', 50000)
        self.rec.process_fill({'order_id': 'bad', 'fill_price': 0, 'size': 0,
                                'ts_filled': time.time()})
        cur = db._conn.execute('SELECT reconciled FROM analytics_fills WHERE order_id="bad"')
        self.assertEqual(cur.fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
