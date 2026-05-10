"""Severity-aware Telegram alert wrapper.

Wraps tg() with structured severity tagging + optional suppression by level.
Existing tg() calls in bot.py remain functional (fully backwards compatible);
new code paths should call alert(severity, ...) for consistency.

Severity levels (ascending priority):
    INFO     — routine events: startup, normal entry/exit, TP hits
    WARNING  — degraded state: cooldown, repeated reconnect, elevated latency
    CRITICAL — needs operator attention soon: CLOSE FAILED, reconcile mismatch,
               unmanaged position, repeated API failures
    FATAL    — trading must stop: risk halt, corrupted state, daily DD halt

Suppression via env var TG_MIN_SEVERITY (default: INFO — send everything).
Set to WARNING to silence routine notifications (still logged).

Public API:
    alert(severity, msg, symbol='', action='', **kwargs)
    info(msg, ...)        # convenience
    warning(msg, ...)
    critical(msg, ...)
    fatal(msg, ...)
"""

import os
from datetime import datetime, timezone

import logger as _logger

_SEVERITY_RANK = {'INFO': 0, 'WARNING': 1, 'CRITICAL': 2, 'FATAL': 3}
_ICONS = {'INFO': 'ℹ️', 'WARNING': '⚠️', 'CRITICAL': '🚨', 'FATAL': '☠️'}

_TG_SENDER = None         # injected by bot.py: alerts.bind_sender(tg_function)
_DB_PERSIST = None        # injected by bot.py: alerts.bind_persist(db.save_analytics_alert)
_MIN_SEVERITY = os.environ.get('TG_MIN_SEVERITY', 'INFO').upper()


def bind_sender(tg_function):
    """Inject the underlying Telegram sender (avoids circular import with bot.py)."""
    global _TG_SENDER
    _TG_SENDER = tg_function


def bind_persist(persist_function):
    """Inject DB persistence callback (signature: dict -> None). Optional."""
    global _DB_PERSIST
    _DB_PERSIST = persist_function


def set_min_severity(level):
    """Programmatic override of TG_MIN_SEVERITY (e.g., for tests)."""
    global _MIN_SEVERITY
    if level.upper() in _SEVERITY_RANK:
        _MIN_SEVERITY = level.upper()


def get_min_severity():
    return _MIN_SEVERITY


def alert(severity, msg, symbol='', action='', **fields):
    """Send a severity-tagged Telegram alert + emit a structured log event.

    Always logs (regardless of suppression). TG send is conditional on
    severity >= TG_MIN_SEVERITY. Failure to send TG never raises.
    """
    sev = (severity or 'INFO').upper()
    if sev not in _SEVERITY_RANK:
        sev = 'INFO'

    # Always log structured event
    try:
        log_fn = (
            _logger.log_error if sev in ('CRITICAL', 'FATAL') else
            _logger.log_warning if sev == 'WARNING' else
            _logger.log_event
        )
        log_fn('alert', severity=sev, msg=msg[:200],
               symbol=symbol or None, action=action or None, **fields)
    except Exception:
        pass

    # Persist to analytics_alerts (best-effort)
    try:
        if _DB_PERSIST is not None:
            _DB_PERSIST({
                'severity': sev, 'msg': msg[:500],
                'symbol': symbol or '', 'action': action or '',
                'details': fields if fields else None,
            })
    except Exception:
        pass

    # Suppression by min severity
    try:
        if _SEVERITY_RANK.get(sev, 0) < _SEVERITY_RANK.get(_MIN_SEVERITY, 0):
            return False
    except Exception:
        pass

    # Format and send
    try:
        if _TG_SENDER is None:
            return False
        ts = datetime.now(timezone.utc).strftime('%H:%M UTC')
        icon = _ICONS.get(sev, '')
        head = f'{icon} <b>{sev}</b> · {ts}'
        body_lines = [head, '━━━━━━━━━━━━━━━━━━━', msg]
        if symbol:
            body_lines.append(f'symbol: <b>{symbol}</b>')
        if action:
            body_lines.append(f'action: <b>{action}</b>')
        text = '\n'.join(body_lines)
        _TG_SENDER(text)
        return True
    except Exception:
        return False


def info(msg, **kwargs):
    return alert('INFO', msg, **kwargs)


def warning(msg, **kwargs):
    return alert('WARNING', msg, **kwargs)


def critical(msg, **kwargs):
    return alert('CRITICAL', msg, **kwargs)


def fatal(msg, **kwargs):
    return alert('FATAL', msg, **kwargs)
