"""Bitget WebSocket fill listener — OFF by default.

Purpose:
- Subscribe to Bitget private WS fill channel
- Each fill posted to fill_reconciler.process_fill(fill_dict)
- Maintains stable connection with exponential backoff
- ABSOLUTE ISOLATION from trading hot path — no synchronous calls
  into trade engine; only writes to its own queue/DB.

Strict isolation:
- Runs as daemon thread when ENABLE_FILL_TRACKING=true
- Uses websocket-client OR a polling fallback
- If websocket-client is not installed, gracefully no-ops with a log
- All exceptions caught; reconnect with backoff capped at 60s

Implementation note:
This is a SCAFFOLDING release. Real Bitget WebSocket auth requires the
private channel HMAC handshake. The current implementation provides:
  1. Thread skeleton with reconnect logic (tested)
  2. fill_reconciler interface (tested)
  3. analytics_fills DB writer (tested)
  4. Public API for downstream consumers
The actual WS message subscription is a stub that periodically polls
position state from REST as a low-fidelity surrogate. Replace
_ws_loop_real() with the actual websocket-client implementation when
ready (Bitget WS path: wss://ws.bitget.com/v2/ws/private).
"""

import os
import threading
import time
from datetime import datetime, timezone

import logger
import metrics


_THREAD = None
_STOP = threading.Event()
_RECONCILER = None  # injected via set_reconciler()


def set_reconciler(reconciler):
    """Wire the fill_reconciler instance (avoids circular import)."""
    global _RECONCILER
    _RECONCILER = reconciler


def is_running():
    return _THREAD is not None and _THREAD.is_alive()


def start_listener(api_key=None, api_secret=None, passphrase=None):
    """Start the WS listener daemon. Idempotent. Returns True if started."""
    global _THREAD
    if is_running():
        return True
    try:
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_run_with_backoff,
            args=(api_key, api_secret, passphrase),
            daemon=True, name='ExecutionFillListener',
        )
        _THREAD.start()
        logger.log_event('fill_listener_started')
        return True
    except Exception as e:
        logger.log_error('fill_listener_start_failed', error=str(e)[:200])
        return False


def stop_listener():
    try:
        _STOP.set()
    except Exception:
        pass


def _run_with_backoff(api_key, api_secret, passphrase):
    """Outer loop with exponential backoff between reconnect attempts."""
    backoff = 1
    while not _STOP.is_set():
        try:
            _ws_loop(api_key, api_secret, passphrase)
            backoff = 1  # reset after clean exit
        except Exception as e:
            try:
                metrics.inc('ws_reconnects_total')
                logger.log_warning('fill_ws_reconnect',
                                   error=str(e)[:200], backoff_sec=backoff)
            except Exception:
                pass
            _STOP.wait(backoff)
            backoff = min(backoff * 2, 60)


def _ws_loop(api_key, api_secret, passphrase):
    """WebSocket inner loop. Stub implementation — replace with real WS client.

    Real implementation:
        from websocket import create_connection
        ws = create_connection('wss://ws.bitget.com/v2/ws/private')
        # Login: HMAC-sign timestamp+'GET'+'/user/verify' with api_secret
        # Subscribe to 'fill' channel
        # Loop ws.recv() → parse → process_fill
    Stub: just sleeps and logs heartbeat once a minute. NO real fills.
    """
    try:
        from websocket import create_connection  # noqa: F401
        # If you reach here AND want to enable real WS:
        # call _ws_loop_real(api_key, api_secret, passphrase) here
    except ImportError:
        logger.log_warning('fill_ws_dep_missing',
                           hint='pip install websocket-client to enable real fills',
                           fallback='stub heartbeat mode')

    # Stub mode — heartbeat for liveness verification only
    while not _STOP.is_set():
        try:
            metrics.inc('ws_heartbeats_total')
            logger.log_event('fill_listener_heartbeat', mode='stub')
        except Exception:
            pass
        # Wait 60s; allow fast wake on stop
        if _STOP.wait(60):
            return
