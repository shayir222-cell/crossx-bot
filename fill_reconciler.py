"""FillReconciler — matches WS fills to outstanding orders + computes slippage.

Public API:
    reconciler = FillReconciler(db_module, metrics_module)
    reconciler.register_pending_order(order_id, symbol, side, expected_price, ts_submitted)
    reconciler.process_fill(fill_dict)   # called by execution_fills WS thread

Slippage computation:
    slippage_pct = (fill_price - expected_price) / expected_price * 100
                   (sign = direction-aware: positive = price went against us for entries)

Failure modes:
    - fill arrives for unknown order_id → counted as orphan, persisted
    - DB write failure → logged, never raises
    - Multiple fills for same order_id → all summed (partial fills supported)

Threading: simple Lock on internal state. No interaction with trading engine.
"""

import threading
import time
from datetime import datetime, timezone

import logger
import metrics


class FillReconciler:
    def __init__(self, db_module, metrics_module=None,
                 pending_ttl_seconds=600):
        self.db = db_module
        self.metrics = metrics_module or metrics
        self._lock = threading.Lock()
        # order_id -> {symbol, side, expected_price, ts_submitted, filled_size, filled_price_avg}
        self._pending = {}
        self._pending_ttl = pending_ttl_seconds

    def register_pending_order(self, order_id, symbol, side,
                                expected_price, ts_submitted=None):
        if not order_id:
            return
        try:
            with self._lock:
                self._pending[order_id] = {
                    'symbol': symbol,
                    'side': side,
                    'expected_price': float(expected_price) if expected_price else 0.0,
                    'ts_submitted': ts_submitted or time.time(),
                    'filled_size': 0.0,
                    'filled_value': 0.0,  # for VWAP across partials
                }
                self._gc_locked()
        except Exception:
            pass

    def _gc_locked(self):
        """Drop orders older than TTL (lock must be held)."""
        cutoff = time.time() - self._pending_ttl
        stale = [oid for oid, p in self._pending.items()
                 if p.get('ts_submitted', 0) < cutoff]
        for oid in stale:
            self._pending.pop(oid, None)

    def process_fill(self, fill):
        """Process a single fill event from WS.
        fill = {
          'order_id': str,
          'symbol': str,
          'side': str,
          'fill_price': float,
          'size': float,
          'ts_filled': float (epoch seconds),
          'raw': {...optional original WS payload...}
        }
        """
        try:
            metrics_mod = self.metrics
            metrics_mod.inc('fills_received_total')

            order_id = fill.get('order_id')
            if not order_id:
                self._record_orphan(fill, reason='no_order_id')
                return

            with self._lock:
                pending = self._pending.get(order_id)

            if pending is None:
                self._record_orphan(fill, reason='unknown_order_id')
                metrics_mod.inc('fills_orphaned_total')
                return

            fp = float(fill.get('fill_price', 0))
            size = float(fill.get('size', 0))
            if fp <= 0 or size <= 0:
                self._record_orphan(fill, reason='bad_price_or_size')
                return

            # Update VWAP
            with self._lock:
                pending['filled_size'] += size
                pending['filled_value'] += fp * size
                vwap = pending['filled_value'] / pending['filled_size'] if pending['filled_size'] > 0 else fp

            # Slippage % (sign-aware: long entry slippage = filled higher than expected = bad)
            expected = pending['expected_price']
            if expected > 0:
                if pending['side'] == 'long':
                    slippage_pct = (fp - expected) / expected * 100
                else:
                    slippage_pct = (expected - fp) / expected * 100
            else:
                slippage_pct = 0.0

            ts_filled = fill.get('ts_filled') or time.time()
            fill_latency_ms = max(0, int((ts_filled - pending['ts_submitted']) * 1000))

            row = {
                'order_id': order_id,
                'symbol': pending['symbol'],
                'side': pending['side'],
                'expected_price': expected,
                'fill_price': fp,
                'size_filled': size,
                'slippage_pct': round(slippage_pct, 4),
                'fill_latency_ms': fill_latency_ms,
                'ack_latency_ms': fill_latency_ms,  # same in stub; refined when REST ack added
                'reconciled': True,
                'raw': fill.get('raw'),
            }
            self.db.save_analytics_fill(row)
            metrics_mod.inc('fills_reconciled_total', symbol=pending['symbol'])
            metrics_mod.add_obs('fill_slippage_pct', slippage_pct,
                                symbol=pending['symbol'])
            metrics_mod.add_obs('fill_latency_ms', fill_latency_ms,
                                symbol=pending['symbol'])

            logger.log_event('fill_reconciled',
                             order_id=order_id, symbol=pending['symbol'],
                             side=pending['side'],
                             expected=expected, fill=fp,
                             slippage_pct=row['slippage_pct'],
                             fill_latency_ms=fill_latency_ms,
                             vwap_to_date=round(vwap, 6))
        except Exception as e:
            try:
                logger.log_error('fill_process_failure', error=str(e)[:200])
            except Exception:
                pass

    def _record_orphan(self, fill, reason='unknown'):
        try:
            row = {
                'order_id': fill.get('order_id') or 'UNKNOWN',
                'symbol': fill.get('symbol') or '',
                'side': fill.get('side') or '',
                'expected_price': None,
                'fill_price': fill.get('fill_price'),
                'size_filled': fill.get('size'),
                'slippage_pct': None,
                'fill_latency_ms': None,
                'ack_latency_ms': None,
                'reconciled': False,
                'raw': {'reason': reason, 'orig': fill.get('raw')},
            }
            self.db.save_analytics_fill(row)
            logger.log_warning('fill_orphan',
                               reason=reason, order_id=row['order_id'],
                               symbol=row['symbol'])
        except Exception:
            pass

    def pending_count(self):
        try:
            with self._lock:
                self._gc_locked()
                return len(self._pending)
        except Exception:
            return 0
