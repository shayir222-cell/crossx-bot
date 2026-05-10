# CrossX Observability — v3.6 (P1)

Production-grade monitoring stack: structured JSON logs, Prometheus metrics, Grafana dashboards, severity-tagged alerts, analytics persistence.

**Trading behaviour is unchanged.** This layer is fully additive — if every observability component fails, the trading loop continues normally.

---

## Architecture

```
┌──────────────────┐
│   bot.py         │
│   ┌──────────┐   │  ┌────────────┐
│   │ webhook  ├───┼──► metrics.py │  in-memory counters/gauges
│   └──────────┘   │  └─────┬──────┘
│   ┌──────────┐   │        │
│   │ monitor  ├───┼────────┘
│   └──────────┘   │  ┌────────────┐
│   ┌──────────┐   │  │ logger.py  │  stdout + logs/json/ + logs/errors/
│   │ reconcile├───┼──┤            │
│   └──────────┘   │  └────────────┘
│   ┌──────────┐   │  ┌────────────┐
│   │ alerts   ├───┼──┤ alerts.py  │  TG severity wrapper (INFO/WARN/CRIT/FATAL)
│   └──────────┘   │  └─────┬──────┘
└──────────────────┘        │
                            ▼
                     ┌────────────────┐
                     │ db.py          │  analytics_signals/trades/alerts
                     └────────────────┘
                            ▲
                     ┌──────┴──────┐
HTTP endpoints:      │ /prometheus │ ──► Prometheus ──► Grafana
                     │ /metrics    │
                     │ /health     │
                     │ /diagnostics│
                     │ /backup     │
                     └─────────────┘
```

## Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /ping` | none | Liveness probe (returns `{status:ok}`) |
| `GET /health` | none | Component readiness summary (uptime monitor friendly) |
| `GET /status` | none | Trading state snapshot (legacy) |
| `GET /metrics` | none | Counters + gauges + observations (JSON) |
| `GET /prometheus` | none | Prometheus text exposition format |
| `GET /signals` | none | Signal log analysis |
| `GET /trades` | none | Trade log |
| `GET /report` | none | Aggregate stats |
| `GET /daily-report` | none | Built daily report text |
| `GET /diagnostics` | **token** | Full runtime snapshot |
| `GET /db-stats` | **token** | DB row counts |
| `GET /backup` | **token** | Full DB snapshot for DR |
| `POST /webhook` | body token | TradingView entry point |

Token endpoints require header `X-Auth-Token: <WEBHOOK_TOKEN>`.

## Event Types (logger.py)

Canonical event names emitted to `logs/json/crossx.log`:

| Event | Level | Trigger |
|-------|-------|---------|
| `startup` | INFO | Bot init |
| `signal_received` | INFO | Webhook accepted |
| `signal_taken` | INFO | All gates passed |
| `signal_rejected` | INFO | Filtered (score, MTF, news, ATR) |
| `signal_paused` / `signal_halted` / `signal_skipped` | INFO/WARN | Pre-gate state |
| `duplicate_prevented` | INFO | Anti-dup hash hit |
| `cooldown_triggered` | INFO | Global/pair cooldown active |
| `order_submitted` | INFO | Bitget order success |
| `order_failed` | ERROR | Bitget rejected order |
| `tp_hit` | INFO | TP1 or TP2 reached |
| `sl_hit` | INFO | Stop loss triggered |
| `trailing_stop` | INFO | Trail stop fired |
| `max_giveback` | INFO | Max giveback exit |
| `time_stop` | INFO | Time stop fired |
| `manual_close` | INFO | /webhook close action |
| `be_set` | INFO | Break-even SL armed |
| `reconcile_started` | INFO | Startup reconcile begin |
| `reconcile_restored` | INFO | Position restored from DB |
| `reconcile_warning` | WARNING | Orphan or unmanaged |
| `reconcile_completed` | INFO | Final summary |
| `reconcile_failure` | ERROR | Live fetch failed |
| `risk_halt` | WARNING | 4-loss streak halt |
| `daily_dd_halt` | ERROR | -10% daily DD halt |
| `close_failed` | ERROR | close_position_safe failed |
| `api_error` | ERROR | Bitget API exception |
| `alert` | varies | alerts.py severity emit |

## Metrics (metrics.py / Prometheus)

All metrics prefixed `crossx_`. Snapshot at `/prometheus` or `/metrics`.

### Counters (monotonic)

```
crossx_signals_received_total
crossx_signals_taken_total
crossx_signals_rejected_total
crossx_duplicate_signals_blocked_total
crossx_cooldowns_triggered_total
crossx_orders_submitted_total
crossx_orders_failed_total
crossx_trades_opened_total
crossx_trades_closed_total
crossx_trades_won_total
crossx_trades_lost_total
crossx_tp_hits_total
crossx_sl_hits_total
crossx_trail_stops_total
crossx_max_giveback_total
crossx_time_stops_total
crossx_manual_closes_total
crossx_reconcile_runs_total
crossx_reconcile_warnings_total
crossx_reconcile_failures_total
crossx_unmanaged_positions_total
crossx_risk_halts_total
crossx_daily_dd_halts_total
crossx_close_fail_total
crossx_api_errors_total
```

### Gauges (current state)

```
crossx_up                        1 if alive
crossx_active_positions          0..N
crossx_current_loss_streak       0..N
crossx_current_win_streak        0..N
crossx_current_drawdown_pct      %
crossx_daily_pnl_pct             %
crossx_daily_peak_pnl_pct        %
crossx_avg_execution_latency_ms  ms
crossx_avg_signal_score          0..100
crossx_session_pnl_asian         %
crossx_session_pnl_london        %
crossx_session_pnl_overlap       %
crossx_session_pnl_ny            %
```

### Observations (avg/min/max/count over sliding 500-event window)

```
crossx_execution_latency_ms_avg / _min / _max / _count
```

## Alerts — Severity Layer

Configured via env `TG_MIN_SEVERITY` (default `INFO`):

| Severity | Examples | Action |
|----------|----------|--------|
| `INFO` | startup, normal entry/exit | Routine — can be suppressed |
| `WARNING` | 2/3 loss streak, reconcile orphan, cooldown overload | Monitor |
| `CRITICAL` | order_failed, close_failed, unmanaged position, repeated API errors | Investigate now |
| `FATAL` | risk halt, daily DD halt, corrupted state | Trading stopped — operator required |

Suppression: `TG_MIN_SEVERITY=WARNING` silences INFO TG sends but still logs everything to file + analytics_alerts table.

Set `TG_MIN_SEVERITY=WARNING` if dual TG messages (legacy + severity) are too noisy. Default is `INFO` (everything sent for now).

## Analytics Tables (db.py — for replay/research)

| Table | Rows per | Purpose |
|-------|----------|---------|
| `analytics_signals` | every webhook | rich signal context (RSI, ATR, streak state, cooldown state, daily pnl, hour/session counts) |
| `analytics_trades` | every close | post-trade analysis (peak_pnl, duration, latency, daily state at open) |
| `analytics_alerts` | every alert() | severity-tagged alert history |

These are ADDITIVE — they never block execution. Failures are silently swallowed.

Backfilling existing trades into `analytics_trades` is not automatic; populated forward from v3.6 deploy.

## Prometheus Setup

### Local testing
```bash
docker run -p 9090:9090 \
  -v "$PWD/infra/prometheus":/etc/prometheus \
  prom/prometheus
```
Open http://localhost:9090 → Graph → `crossx_up` → expect 1.

### Production
1. Run Prometheus alongside the bot OR use Render/external Prometheus
2. Use [`infra/prometheus/prometheus.yml`](infra/prometheus/prometheus.yml) as template
3. Optionally enable rules: [`infra/prometheus/rules/kpi.yml`](infra/prometheus/rules/kpi.yml)

### Authenticating /prometheus
Currently `/prometheus` is unauthenticated (Prometheus convention).
If you scrape from a public Prometheus instance, consider:
- IP-allowlist via Render/Cloudflare in front
- OR add `_check_auth` to `prometheus_endpoint` in bot.py and configure
  `basic_auth` / `authorization` in the scrape config.

## Grafana Setup

1. Add data source: Prometheus pointing at your scrape target
2. Dashboards → Import JSON → upload each from [`infra/grafana/dashboards/`](infra/grafana/dashboards/):
   - `1-trading-overview.json` — balance, PnL, signals, win rate
   - `2-risk-monitor.json` — halts, drawdown, cooldowns, exit reasons
   - `3-infrastructure-health.json` — API errors, reconcile, halts
   - `4-execution-diagnostics.json` — latency, scores, throughput

## Smoke Tests

```bash
# Liveness
curl -fsS https://crossx-bot.onrender.com/ping
curl -fsS https://crossx-bot.onrender.com/health | jq

# Metrics (no auth)
curl -fsS https://crossx-bot.onrender.com/metrics | jq '.counters'
curl -fsS https://crossx-bot.onrender.com/prometheus | head -40

# Auth-required
TOKEN=<WEBHOOK_TOKEN>
curl -fsS -H "X-Auth-Token: $TOKEN" https://crossx-bot.onrender.com/diagnostics | jq '.metrics.gauges'
```

## Failure Modes (designed)

| Component fails | Effect on trading | Effect on observability |
|-----------------|-------------------|--------------------------|
| `logger.py` setup error | None — print fallback | Logs to stdout only (no file) |
| `metrics.py` bad input | None — silently swallowed | That metric stays at last value |
| `db.py` DB outage | None — `_safe` decorator | Persistence skipped; in-memory continues |
| `alerts.py` sender fails | None | Alert logged, not sent to TG |
| Prometheus scrape error | None | Stale dashboards |
| /diagnostics auth fail | None | 401 returned |

## Rollback

Each phase = one commit. To revert just observability:
```bash
git revert <commit-hash>      # of v3.6 phase
git push origin main
```

## File Layout

```
crossx-bot/
├── bot.py                  # main app (modified)
├── db.py                   # SQLite (additions: analytics_*)
├── logger.py               # NEW — structured event logger
├── metrics.py              # NEW — counters/gauges
├── alerts.py               # NEW — severity wrapper
├── infra/
│   ├── prometheus/
│   │   ├── prometheus.yml
│   │   └── rules/kpi.yml
│   └── grafana/
│       └── dashboards/
│           ├── 1-trading-overview.json
│           ├── 2-risk-monitor.json
│           ├── 3-infrastructure-health.json
│           └── 4-execution-diagnostics.json
└── OBSERVABILITY.md        # this file
```
