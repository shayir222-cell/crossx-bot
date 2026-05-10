"""Unit tests for metrics.py — label support, cardinality, percentiles."""
import os, tempfile, sys, unittest

# Bootstrap module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics as M


class TestMetricsLabels(unittest.TestCase):
    def setUp(self):
        M.reset()

    def test_unlabeled_inc_returns_scalar(self):
        M.inc('foo')
        M.inc('foo', 5)
        s = M.snapshot()
        self.assertEqual(s['counters']['foo'], 6)

    def test_labeled_inc_returns_list(self):
        M.inc('trades', symbol='BTC')
        M.inc('trades', symbol='BTC')
        M.inc('trades', symbol='ETH')
        s = M.snapshot()
        v = s['counters']['trades']
        self.assertIsInstance(v, list)
        btc = next(x for x in v if x['labels'].get('symbol') == 'BTC')
        eth = next(x for x in v if x['labels'].get('symbol') == 'ETH')
        self.assertEqual(btc['value'], 2)
        self.assertEqual(eth['value'], 1)

    def test_cardinality_cap(self):
        for i in range(M.MAX_LABELSETS_PER_METRIC + 5):
            M.inc('big', symbol=f'S{i}')
        s = M.snapshot()
        self.assertLessEqual(len(s['counters']['big']), M.MAX_LABELSETS_PER_METRIC)
        self.assertGreaterEqual(s['cardinality_dropped'].get('big', 0), 5)

    def test_label_value_truncation(self):
        long_val = 'x' * 200
        M.inc('lg', tag=long_val)
        flat = M.list_counters()
        for name, ls, _ in flat:
            if name == 'lg':
                self.assertEqual(len(dict(ls)['tag']), 64)
                return
        self.fail('lg not found')

    def test_reserved_labels_dropped(self):
        M.inc('test', __name__='xxx', job='yyy', symbol='BTC')
        flat = M.list_counters()
        for name, ls, _ in flat:
            if name == 'test':
                d = dict(ls)
                self.assertNotIn('__name__', d)
                self.assertNotIn('job', d)
                self.assertEqual(d.get('symbol'), 'BTC')

    def test_percentile_calculation(self):
        # Standard nearest-rank method may yield ±1 at boundary indices
        for i in range(1, 101):
            M.add_obs('lat', i)
        self.assertAlmostEqual(M.percentile('lat', 50), 50, delta=2)
        self.assertAlmostEqual(M.percentile('lat', 95), 95, delta=2)
        self.assertAlmostEqual(M.percentile('lat', 99), 99, delta=2)

    def test_observations_with_labels(self):
        M.add_obs('latency', 100, stage='parse')
        M.add_obs('latency', 200, stage='parse')
        M.add_obs('latency', 50, stage='exec')
        s = M.snapshot()
        obs = s['observations']['latency']
        self.assertIsInstance(obs, list)
        parse_obs = next(x for x in obs if x['labels'].get('stage') == 'parse')
        self.assertAlmostEqual(parse_obs['avg'], 150)

    def test_bad_input_does_not_raise(self):
        M.inc('bad', n=None)
        M.gauge('bad2', 'not_a_number')
        M.add_obs('bad3', 'bad')
        # State should still be queryable
        self.assertIsInstance(M.snapshot(), dict)

    def test_init_canonical_idempotent(self):
        M.init_canonical()
        n1 = len(M.snapshot()['counters'])
        M.init_canonical()
        n2 = len(M.snapshot()['counters'])
        self.assertEqual(n1, n2)


if __name__ == '__main__':
    unittest.main()
