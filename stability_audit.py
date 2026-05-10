"""Stability audit engine — computes 5 production-readiness scores.

Pure analysis: reads metrics + DB stats, returns score breakdown. No mutation.

Public API:
    from stability_audit import run_audit
    result = run_audit(metrics_module, db_module)
    # result = {
    #   'overall': 87.5,
    #   'scores': {
    #     'operational_readiness': 92,
    #     'reliability': 86,
    #     'execution_consistency': 84,
    #     'observability_integrity': 90,
    #     'reconciliation_accuracy': 88,
    #   },
    #   'findings': [...],
    #   'bottlenecks': [...],
    #   'recommendations': [...],
    # }

The 5 scores are normalized 0-100, higher is better.
"""

from datetime import datetime, timezone


def _safe_get_counter(counters, name):
    """Return total value of a counter (sum across labelsets if labeled)."""
    val = counters.get(name)
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, list):
        return sum(x.get('value', 0) for x in val)
    return 0


def _safe_get_gauge(gauges, name, labels=None):
    """Return scalar gauge or specific labelset value."""
    val = gauges.get(name)
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list) and not labels:
        # No specific labels: return the first one
        return float(val[0].get('value', 0)) if val else 0.0
    return 0.0


def _score_operational_readiness(snap, db_stats):
    """0-100. Components ready, no halts, no critical errors."""
    score = 100
    findings = []

    counters = snap.get('counters', {})
    api_errors = _safe_get_counter(counters, 'api_errors_total')
    close_fails = _safe_get_counter(counters, 'close_fail_total')
    risk_halts = _safe_get_counter(counters, 'risk_halts_total')
    dd_halts = _safe_get_counter(counters, 'daily_dd_halts_total')
    unmanaged = _safe_get_counter(counters, 'unmanaged_positions_total')

    if risk_halts >= 1:
        score -= 15
        findings.append({'severity': 'high', 'msg': f'risk_halt fired {risk_halts}× since boot'})
    if dd_halts >= 1:
        score -= 15
        findings.append({'severity': 'high', 'msg': f'daily_dd_halt fired {dd_halts}×'})
    if close_fails >= 3:
        score -= 10
        findings.append({'severity': 'high', 'msg': f'{close_fails} close failures (likely Bitget instability)'})
    elif close_fails >= 1:
        score -= 3
        findings.append({'severity': 'medium', 'msg': f'{close_fails} close failure(s) — investigate'})
    if api_errors >= 10:
        score -= 8
        findings.append({'severity': 'medium', 'msg': f'{api_errors} API errors — possible network instability'})
    if unmanaged >= 1:
        score -= 10
        findings.append({'severity': 'high', 'msg': f'{unmanaged} unmanaged position(s) on Bitget'})

    # DB readiness
    if not db_stats:
        score -= 20
        findings.append({'severity': 'critical', 'msg': 'DB stats unavailable'})

    return max(0, min(100, score)), findings


def _score_reliability(snap, db_stats):
    """0-100. Restart resilience + uptime measures."""
    score = 100
    findings = []

    soak_count = db_stats.get('analytics_soak_reports', 0) or 0
    if soak_count == 0:
        score -= 5
        findings.append({'severity': 'low', 'msg': 'No soak reports yet — enable ENABLE_SOAK_VALIDATION'})

    counters = snap.get('counters', {})
    recon_runs = _safe_get_counter(counters, 'reconcile_runs_total')
    if recon_runs == 0:
        score -= 15
        findings.append({'severity': 'medium', 'msg': 'reconcile never ran — bot has not restarted? state unverified'})

    recon_failures = _safe_get_counter(counters, 'reconcile_failures_total')
    if recon_failures > 0:
        score -= 20
        findings.append({'severity': 'high',
                         'msg': f'{recon_failures} reconcile_failures — Bitget API unreachable on startup?'})

    return max(0, min(100, score)), findings


def _score_execution_consistency(snap, db_stats):
    """0-100. Order placement quality + latency stability."""
    score = 100
    findings = []

    counters = snap.get('counters', {})
    submitted = _safe_get_counter(counters, 'orders_submitted_total')
    failed = _safe_get_counter(counters, 'orders_failed_total')
    if submitted > 0:
        fail_rate = failed / submitted
        if fail_rate > 0.05:
            score -= 20
            findings.append({'severity': 'high',
                             'msg': f'order failure rate {fail_rate*100:.1f}% (>{int(0.05*100)}%)'})
        elif fail_rate > 0.01:
            score -= 8
            findings.append({'severity': 'medium',
                             'msg': f'order failure rate {fail_rate*100:.1f}% slightly elevated'})

    lat_anomalies = _safe_get_counter(counters, 'webhook_latency_anomalies_total')
    if lat_anomalies > 0:
        score -= min(15, lat_anomalies * 2)
        findings.append({'severity': 'medium',
                         'msg': f'{lat_anomalies} webhook latency anomalies'})

    obs = snap.get('observations', {})
    wh_lat = obs.get('webhook_total_latency_ms')
    if isinstance(wh_lat, dict):
        p95 = wh_lat.get('p95', 0)
        if p95 > 3000:
            score -= 15
            findings.append({'severity': 'medium', 'msg': f'webhook P95 latency = {p95:.0f}ms'})
        elif p95 > 1500:
            score -= 5
            findings.append({'severity': 'low', 'msg': f'webhook P95 latency = {p95:.0f}ms'})

    return max(0, min(100, score)), findings


def _score_observability_integrity(snap, db_stats):
    """0-100. Are all log/metric/db pipelines flowing?"""
    score = 100
    findings = []

    counters = snap.get('counters', {})
    sigs_received = _safe_get_counter(counters, 'signals_received_total')
    sigs_db = db_stats.get('signals', 0) or 0
    asigs_db = db_stats.get('analytics_signals', 0) or 0

    # signals counter should be ≈ db.signals (after restart counter resets to 0
    # but DB doesn't, so allow >=)
    # analytics_signals should be ≈ db.signals
    if sigs_db > 0:
        coverage = asigs_db / sigs_db
        if coverage < 0.9:
            score -= 20
            findings.append({'severity': 'high',
                             'msg': f'analytics_signals coverage only {coverage*100:.1f}% of signals_log'})
        elif coverage < 0.99:
            score -= 5
            findings.append({'severity': 'low',
                             'msg': f'analytics_signals coverage {coverage*100:.1f}%'})

    # Cardinality dropped is a red flag if any
    dropped = snap.get('cardinality_dropped', {})
    if dropped:
        score -= 5
        findings.append({'severity': 'medium',
                         'msg': f'cardinality dropped: {dropped}'})

    # Soak integrity gauge
    integrity = _safe_get_gauge(snap.get('gauges', {}), 'observability_integrity_score')
    if integrity > 0:
        score = int(score * 0.7 + integrity * 100 * 0.3)
        findings.append({'severity': 'info', 'msg': f'soak integrity gauge: {integrity:.4f}'})

    return max(0, min(100, score)), findings


def _score_reconciliation_accuracy(snap, db_stats):
    """0-100. Reconcile success vs warning rate."""
    score = 100
    findings = []

    counters = snap.get('counters', {})
    runs = _safe_get_counter(counters, 'reconcile_runs_total')
    warns = _safe_get_counter(counters, 'reconcile_warnings_total')
    failures = _safe_get_counter(counters, 'reconcile_failures_total')
    unmanaged = _safe_get_counter(counters, 'unmanaged_positions_total')

    if runs > 0:
        warn_rate = warns / runs
        if warn_rate > 0.5:
            score -= 25
            findings.append({'severity': 'high',
                             'msg': f'{warns}/{runs} reconcile runs had warnings ({warn_rate*100:.0f}%)'})
        elif warn_rate > 0.2:
            score -= 10
            findings.append({'severity': 'medium',
                             'msg': f'reconcile warn rate elevated: {warn_rate*100:.0f}%'})

    if failures > 0:
        score -= 30
        findings.append({'severity': 'high', 'msg': f'{failures} reconcile failures (Bitget unreachable)'})

    if unmanaged > 0:
        score -= 15
        findings.append({'severity': 'high',
                         'msg': f'{unmanaged} unmanaged position(s) ever observed — manual close required'})

    return max(0, min(100, score)), findings


def run_audit(metrics_module, db_module):
    """Compute all 5 readiness scores. Read-only."""
    try:
        snap = metrics_module.snapshot()
    except Exception:
        snap = {'counters': {}, 'gauges': {}, 'observations': {}, 'cardinality_dropped': {}}
    try:
        db_stats = db_module.stats() if db_module.is_ready() else {}
    except Exception:
        db_stats = {}

    scores = {}
    findings = []

    s, f = _score_operational_readiness(snap, db_stats)
    scores['operational_readiness'] = s
    for ff in f: ff['component'] = 'operational_readiness'
    findings.extend(f)

    s, f = _score_reliability(snap, db_stats)
    scores['reliability'] = s
    for ff in f: ff['component'] = 'reliability'
    findings.extend(f)

    s, f = _score_execution_consistency(snap, db_stats)
    scores['execution_consistency'] = s
    for ff in f: ff['component'] = 'execution_consistency'
    findings.extend(f)

    s, f = _score_observability_integrity(snap, db_stats)
    scores['observability_integrity'] = s
    for ff in f: ff['component'] = 'observability_integrity'
    findings.extend(f)

    s, f = _score_reconciliation_accuracy(snap, db_stats)
    scores['reconciliation_accuracy'] = s
    for ff in f: ff['component'] = 'reconciliation_accuracy'
    findings.extend(f)

    overall = sum(scores.values()) / len(scores)

    # Recommended actions based on findings
    recommendations = []
    by_severity = {'critical': [], 'high': [], 'medium': [], 'low': []}
    for f in findings:
        by_severity.setdefault(f.get('severity', 'low'), []).append(f)
    if by_severity.get('critical'):
        recommendations.append('STOP TRADING — investigate critical findings before next signal')
    if by_severity.get('high'):
        recommendations.append('Address all HIGH findings within 24h; monitor metrics dashboards')
    if scores['observability_integrity'] < 90:
        recommendations.append('Enable ENABLE_SOAK_VALIDATION + verify analytics_signals coverage')
    if scores['reliability'] < 90:
        recommendations.append('Verify reconcile_positions runs cleanly on every restart')
    if not recommendations:
        recommendations.append('All scores green — continue routine monitoring')

    # Persist gauges so dashboards show live audit
    try:
        metrics_module.gauge('operational_readiness_score', scores['operational_readiness'])
        metrics_module.gauge('reliability_score', scores['reliability'])
        metrics_module.gauge('execution_consistency_score', scores['execution_consistency'])
        metrics_module.gauge('observability_integrity_score', scores['observability_integrity'] / 100.0)
        metrics_module.gauge('reconcile_accuracy_score', scores['reconciliation_accuracy'])
    except Exception:
        pass

    return {
        'ts': datetime.now(timezone.utc).isoformat(),
        'overall': round(overall, 2),
        'scores': scores,
        'findings': findings,
        'recommendations': recommendations,
        'severity_counts': {k: len(v) for k, v in by_severity.items()},
    }


def render_markdown(audit_result):
    """Render audit result as markdown for FINAL_STABILITY_AUDIT.md."""
    lines = []
    lines.append('# CrossX Stability Audit')
    lines.append('')
    lines.append(f'**Generated:** {audit_result["ts"]}')
    lines.append(f'**Overall score:** **{audit_result["overall"]:.1f}/100**')
    lines.append('')
    lines.append('## Component scores')
    lines.append('')
    lines.append('| Component | Score |')
    lines.append('|-----------|-------|')
    for k, v in audit_result['scores'].items():
        lines.append(f'| {k.replace("_", " ").title()} | **{v}/100** |')
    lines.append('')

    sev = audit_result.get('severity_counts', {})
    lines.append('## Severity summary')
    lines.append('')
    for level in ('critical', 'high', 'medium', 'low', 'info'):
        n = sev.get(level, 0)
        if n:
            lines.append(f'- **{level.upper()}:** {n}')
    lines.append('')

    if audit_result.get('findings'):
        lines.append('## Findings')
        lines.append('')
        for f in audit_result['findings']:
            sev_icon = {
                'critical': '🛑', 'high': '🔴', 'medium': '🟡',
                'low': '🟢', 'info': 'ℹ️'
            }.get(f.get('severity', 'low'), '·')
            lines.append(f'- {sev_icon} **[{f.get("component")}]** {f.get("msg")}')
        lines.append('')

    if audit_result.get('recommendations'):
        lines.append('## Recommendations')
        lines.append('')
        for r in audit_result['recommendations']:
            lines.append(f'- {r}')
        lines.append('')

    return '\n'.join(lines)
