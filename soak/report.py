"""Soak report rendering — converts persisted analytics_soak_reports into
markdown/JSON for ops review.

Used by:
- /soak-report endpoint (token-auth)
- daily ops cron / manual operator query
"""

import json


def build_report(soak_row):
    """Render single soak row as markdown table snippet."""
    lines = []
    lines.append('## Soak Report')
    lines.append('')
    lines.append(f'- **Duration:** {soak_row.get("duration_min", 0)} min')
    lines.append(f'- **Webhook uptime:** {soak_row.get("webhook_uptime_pct", 0):.2f}%')
    lines.append(f'- **Metrics uptime:** {soak_row.get("metrics_uptime_pct", 0):.2f}%')
    lines.append(f'- **Diagnostics uptime:** {soak_row.get("diagnostics_uptime_pct", 0):.2f}%')
    lines.append(f'- **Prometheus uptime:** {soak_row.get("prometheus_uptime_pct", 0):.2f}%')
    lines.append('')
    lines.append('### Growth')
    lines.append(f'- Signals: +{soak_row.get("signals_growth", 0)}')
    lines.append(f'- Analytics signals: +{soak_row.get("analytics_signals_growth", 0)}')
    lines.append(f'- Analytics trades: +{soak_row.get("analytics_trades_growth", 0)}')
    lines.append(f'- Reconcile runs observed: {soak_row.get("reconcile_runs_observed", 0)}')
    lines.append('')
    lines.append('### Scores')
    lines.append(f'- Event continuity: **{soak_row.get("event_continuity_score", 0):.4f}**')
    lines.append(f'- Observability integrity: **{soak_row.get("observability_integrity_score", 0):.4f}**')
    lines.append('')

    anomalies = soak_row.get('anomalies', []) or []
    if isinstance(anomalies, str):
        try:
            anomalies = json.loads(anomalies)
        except Exception:
            anomalies = []
    if anomalies:
        lines.append(f'### Anomalies ({len(anomalies)})')
        kinds = {}
        for a in anomalies:
            k = a.get('kind', '?') if isinstance(a, dict) else str(a)
            kinds[k] = kinds.get(k, 0) + 1
        for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
            lines.append(f'- {k}: ×{n}')
    else:
        lines.append('### Anomalies')
        lines.append('- None ✅')

    return '\n'.join(lines)


def summarize_recent(rows):
    """Aggregate over multiple soak rows."""
    if not rows:
        return {
            'count': 0,
            'avg_uptime_pct': 0.0,
            'avg_continuity': 0.0,
            'avg_integrity': 0.0,
            'total_anomalies': 0,
        }
    avg_up = sum(r.get('webhook_uptime_pct', 0) for r in rows) / len(rows)
    avg_cont = sum(r.get('event_continuity_score', 0) for r in rows) / len(rows)
    avg_int = sum(r.get('observability_integrity_score', 0) for r in rows) / len(rows)
    total_anom = 0
    for r in rows:
        a = r.get('anomalies', [])
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except Exception:
                a = []
        total_anom += len(a) if isinstance(a, list) else 0
    return {
        'count': len(rows),
        'avg_uptime_pct': round(avg_up, 2),
        'avg_continuity': round(avg_cont, 4),
        'avg_integrity': round(avg_int, 4),
        'total_anomalies': total_anom,
    }
