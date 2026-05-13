# CrossX Bot — ROADMAP

Baseline document. Living guide for decisions. Updated only at phase transitions.

---

## ROLE

Quant practitioner, working with retail capital ($100-500) on consumer
infrastructure (Render, FastAPI, Bitget, TradingView).

Main principle:
**find edge first — build infra around it.**
Never the reverse.

Mindset:
- think in expectancy, Sharpe, drawdown — NOT "% per day"
- validate every hypothesis on out-of-sample data
- if strategy is losing — say so and rollback
- don't mask losses with features, UI, ML, buzzwords
- don't add complexity until edge is proven

Not a yes-man. Reformulate unrealistic goals. Drop features that don't move the metric.

---

## HONEST STARTING POINT (2026-05-13)

| | |
|---|---|
| Capital | $101.78 |
| Pairs | BTC, ETH, BNB, XRP, TON, LINK, AVAX (7) |
| Stack | Python 3.12 + FastAPI + SQLite + Bitget V2 + TV webhooks |
| Hosting | Render single instance |
| Backtest 30d | **-0.305R expectancy (LOSING)** |
| Calibration | PAIR_TP_R + PAIR_MIN_SCORE applied, not yet validated in live |
| Profitable (backtest) | TON, AVAX only |

Starting position. Goal — reach +EV, not build "AI OS".

---

## PRIMARY GOAL — measurable, not aspirational

Achieve on a 90-day **LIVE** window (not backtest):

| Metric | Target |
|---|---|
| Expectancy | > +0.10 R / trade |
| Sharpe ratio | > 1.0 |
| Max drawdown | < 20% |
| Profit Factor | > 1.3 |
| Sample size | >= 100 trades |

Equivalent to ~30-60% annualized risk-adjusted.
Good prop trader level.

Goal is **NOT** "X% per day". Daily return is a derivative metric.

---

## FORBIDDEN

- daily return targets ("X% per day")
- stop-on-target ("hit 2% — wait till tomorrow")
- martingale, averaging down, no stop-loss
- leverage > 10x
- auto-training without walk-forward + out-of-sample
- ML/AI features before baseline edge is proven
- dashboard, Grafana, Postgres, Docker — before Phase 4
- working multiple phases in parallel

---

## PHASES — gated by validation

### Phase 0 — EDGE VALIDATION (CURRENT)

- Target: 50+ live trades, expectancy > 0
- Let bot collect data with current calibration
- Every 7 days: `/perf` analysis
- **Gate:**
  - if after 50 trades expectancy <= 0 → Phase 0.5
  - if > +0.05R → Phase 1

### Phase 0.5 — STRATEGY REBUILD (only if Phase 0 failed)

Deep analysis of losing clusters:
- hour of day
- regime (ADX low/high)
- funding rate
- score band (92 vs 100)

Actions:
- remove conditions with -EV (conditions, NOT pairs)
- possibly switch base logic (mean-reversion instead of ATR trend, or break-of-structure)

**Gate:** new code's 30d backtest > +0.10R

### Phase 1 — EDGE PROTECTION

When Phase 0 shows +EV in live.

Add one feature at a time, measure each:
- funding rate filter
- correlation block (BTC+ETH not simultaneously)
- volatility regime (skip ATR pct > 95)
- time-of-day filter (if stats show dead hours)

Soak test: 14 days without param changes.

**Gate:** Sharpe > 1.0 over 90 trades

### Phase 2 — SIZING OPTIMIZATION

When Phase 1 passed.

- adaptive risk per regime (low vol 0.7%, high vol 0.3%)
- loss streak throttle (3 losses → 4h pause)
- Kelly fraction sizing, capped at 0.25 of full Kelly

**Gate:** expectancy > +0.15R over 100+ trades

### Phase 3 — CAPITAL SCALING

When Phase 2 holds steady at +0.15R.

- capital → $500
- slippage measurement (expected vs filled)
- Bybit as failover exchange

**Gate:** metrics hold at $500

### Phase 4 — INFRASTRUCTURE HARDENING

Only after Phase 0-3 complete.

- Postgres replacing SQLite
- Grafana / Prometheus
- minimal web dashboard (NOT Bloomberg)
- walk-forward parameter optimization
- possibly multi-strategy router

---

## WHAT TO LOG (already implemented — don't expand yet)

Per trade: `symbol, side, entry_time, entry_price, exit_time, exit_price, score, R, ADX, funding_rate, session`

Enough for Phase 0-2. MAE/MFE/slippage deferred to Phase 1. Don't build "future-proof" tables.

---

## HARD STOP RULES

Halt trading and review if:
- drawdown > 30% of starting capital
- 200+ live trades with expectancy < -0.05R
- Bitget API error rate > 5% over 24h
- regulatory change in user's jurisdiction

---

## DEFINITION OF DONE (per phase)

A phase is NOT done when "code is written".
A phase is done when the **metric improved on out-of-sample data**.

Code written + metric unchanged = **FAIL → rollback.**
No "keep for later", no "might be useful". Rollback. Next.

---

## DECISION POLICY

Every proposal scored on 3 criteria:
1. Will it improve expectancy / Sharpe / DD?
2. Can it be validated on current data?
3. Will it break something already working?

If any answer is "no" — don't do it.

---

## COMMUNICATION

Weekly report contains:
- weekly expectancy
- cumulative metrics
- code changes since last report
- proposed next step
- what was REJECTED (and why)

Transparency over polish.

---

## CHANGELOG

- **2026-05-13** — initial draft. Phase 0 active. Awaiting first 50 live trades on commit `cc4d9a8`.
