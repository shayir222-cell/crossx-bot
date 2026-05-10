"""Structured JSON event logger for CrossX Bot.

Design principles:
- Fail-safe: a logger failure must NEVER crash trading. All public functions
  catch all exceptions and degrade silently (with one stderr print).
- Idempotent: setup_logger() can be called multiple times; second call no-ops.
- Stdout-first: even if filesystem ops fail (read-only mount, no disk), we
  still emit to stdout (Render captures it).
- File rotation: daily rotation, 14 days for INFO logs, 30 days for errors.
- Schema: { ts: ISO-8601 UTC, lvl, evt, ...fields, msg? }

Public API:
    setup_logger(log_dir='logs')           # call once at startup
    log_event(evt, **fields)               # INFO-level structured event
    log_warning(evt, **fields)             # WARNING
    log_error(evt, **fields)               # ERROR

Event types (canonical list — see CHANGELOG):
    startup, signal_received, signal_rejected, signal_taken,
    duplicate_prevented, cooldown_triggered, order_submitted,
    order_failed, tp_hit, sl_hit, trailing_stop, max_giveback,
    time_stop, be_set, manual_close, reconcile_started,
    reconcile_restored, reconcile_warning, reconcile_completed,
    risk_halt, daily_dd_halt, close_failed, api_error
"""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

_LOGGER = None
_INITIALIZED = False
_LOG_DIR = None


class _PassthroughFormatter(logging.Formatter):
    """Message is already JSON; emit verbatim."""
    def format(self, record):
        return record.getMessage()


def setup_logger(log_dir='logs'):
    """Idempotent. Returns the configured logger or None on catastrophic failure."""
    global _LOGGER, _INITIALIZED, _LOG_DIR
    if _INITIALIZED:
        return _LOGGER

    try:
        logger = logging.getLogger('crossx.events')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()  # avoid double-handlers across re-setup

        fmt = _PassthroughFormatter()

        # Always-on stdout (Render console captures this)
        h_out = logging.StreamHandler()
        h_out.setFormatter(fmt)
        h_out.setLevel(logging.INFO)
        logger.addHandler(h_out)

        # File handlers — best-effort
        try:
            os.makedirs(os.path.join(log_dir, 'json'), exist_ok=True)
            os.makedirs(os.path.join(log_dir, 'errors'), exist_ok=True)

            h_json = TimedRotatingFileHandler(
                os.path.join(log_dir, 'json', 'crossx.log'),
                when='midnight', backupCount=14, utc=True, encoding='utf-8',
            )
            h_json.setFormatter(fmt)
            h_json.setLevel(logging.INFO)
            logger.addHandler(h_json)

            h_err = TimedRotatingFileHandler(
                os.path.join(log_dir, 'errors', 'error.log'),
                when='midnight', backupCount=30, utc=True, encoding='utf-8',
            )
            h_err.setFormatter(fmt)
            h_err.setLevel(logging.WARNING)
            logger.addHandler(h_err)
            _LOG_DIR = log_dir
        except Exception as e:
            try:
                print(f'[LOGGER] file handlers disabled: {e}')
            except Exception:
                pass

        _LOGGER = logger
        _INITIALIZED = True
        return logger
    except Exception as e:
        try:
            print(f'[LOGGER] setup failed: {e}')
        except Exception:
            pass
        _INITIALIZED = True  # don't retry forever
        return None


def is_ready():
    return _LOGGER is not None


def log_dir():
    return _LOG_DIR


def _emit(level, evt, fields):
    try:
        if _LOGGER is None:
            # Fall back to stdout JSON line so events aren't completely lost
            try:
                payload = {
                    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                    'lvl': level, 'evt': evt,
                }
                payload.update({k: v for k, v in fields.items() if v is not None})
                print(json.dumps(payload, ensure_ascii=False, default=str))
            except Exception:
                pass
            return

        payload = {
            'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'lvl': level,
            'evt': evt,
        }
        for k, v in fields.items():
            if v is not None:
                payload[k] = v
        msg = json.dumps(payload, ensure_ascii=False, default=str)

        if level in ('ERROR', 'CRITICAL'):
            _LOGGER.error(msg)
        elif level == 'WARNING':
            _LOGGER.warning(msg)
        else:
            _LOGGER.info(msg)
    except Exception:
        # Last-ditch: never raise from logger
        try:
            print(f'[LOG-FAIL] {evt}')
        except Exception:
            pass


def log_event(evt, **fields):
    _emit('INFO', evt, fields)


def log_warning(evt, **fields):
    _emit('WARNING', evt, fields)


def log_error(evt, **fields):
    _emit('ERROR', evt, fields)
