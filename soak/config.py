"""Soak validator configuration. Pure dataclass — no side effects on import."""

import os


class SoakConfig:
    """Reads environment with safe defaults. All attributes have stable types."""

    def __init__(self):
        # Polling interval — how often to probe endpoints (seconds)
        self.tick_seconds = int(os.environ.get('SOAK_TICK_SECONDS', '60'))

        # Report cadence — how often to compute & persist summary
        self.report_every_minutes = int(os.environ.get('SOAK_REPORT_EVERY_MINUTES', '60'))

        # Self-probe URL base (for endpoints other than via Python imports)
        self.base_url = os.environ.get('SOAK_BASE_URL', '').strip()

        # Auth token for /diagnostics + /db-stats
        self.auth_token = os.environ.get('WEBHOOK_TOKEN', '')

        # Anomaly thresholds
        self.expected_signals_per_hour_min = int(os.environ.get('SOAK_MIN_SIGNALS_PER_HOUR', '0'))
        self.uptime_alert_threshold_pct = float(os.environ.get('SOAK_UPTIME_ALERT_PCT', '99.0'))
        self.continuity_alert_threshold = float(os.environ.get('SOAK_CONTINUITY_THRESHOLD', '0.95'))
        self.latency_p95_alert_ms = int(os.environ.get('SOAK_P95_ALERT_MS', '3000'))

        # Sanity checks at startup
        self.tick_seconds = max(10, min(self.tick_seconds, 600))
        self.report_every_minutes = max(5, min(self.report_every_minutes, 1440))

    def to_dict(self):
        return {
            'tick_seconds': self.tick_seconds,
            'report_every_minutes': self.report_every_minutes,
            'base_url': self.base_url or '(local-only)',
            'expected_signals_per_hour_min': self.expected_signals_per_hour_min,
            'uptime_alert_threshold_pct': self.uptime_alert_threshold_pct,
            'continuity_alert_threshold': self.continuity_alert_threshold,
            'latency_p95_alert_ms': self.latency_p95_alert_ms,
        }
