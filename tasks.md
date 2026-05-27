# CrossX — Edge Boost Tasks

Generated 2026-05-27 by orchestrated offline research (4 sub-agents: data map, BNB forensics, TON characterization, top-10 audition, selection methodology). All data sourced from Bitget public 5m candles + local DB.

**Soak window active.** Tasks split into:
- **SOAK-SAFE** — offline tools, backtests, analytics. No prod-param changes. Executable now.
- **POST-SOAK** — production changes. Gated on end of Phase 0 validation (50 live trades or 2026-05-23 checkpoint passed).

## Workflow per task (user-specified)
1. Pre-task: spawn analyst sub-agent → list pitfalls, edge cases, problematic spots.
2. Execute task.
3. Post-task: spawn 2+ reviewer sub-agents in parallel → rate 1-10, suggest fixes.
4. Loop reviews until at least one rates ≥ 9.5/10.
5. Apply agreed fixes → commit → mark done → next task.

---

## Background data (read these first, do not re-derive)

### Top-10 audition results (TP_R=2.0, 30d, MIN_SCORE=92, optimistic upper bound)

| Symbol | exp_R | PF | WR% | L/S balance | DD% | Verdict |
|--------|------:|---:|----:|-------------|----:|---------|
| **ZECUSDT** | **+0.294** | 1.57 | 49.7 | 55/45 | **17** | **Best** |
| **HYPEUSDT** | +0.211 | 1.38 | 48.4 | 60/40 | 30 | Good |
| **DOGEUSDT** | +0.127 | 1.21 | 47.8 | 24/76 | 48 | Borderline |
| XRPUSDT | -0.079 | 0.88 | 43.8 | 21/79 | 82 | Reject |
| ADAUSDT | -0.052 | 0.92 | 42.5 | 21/79 | 106 | Reject |
| SOLUSDT | -0.115 | 0.82 | 40.8 | 23/77 | 82 | Reject |
| BNBUSDT | -0.228 | 0.72 | 40.2 | 51/49 | 103 | **Drop** |
| ETHUSDT | -0.272 | 0.64 | 37.4 | **9/91** | 165 | Reject |
| BTCUSDT | -0.453 | 0.48 | 33.5 | 19/81 | 225 | Reject |
| TRXUSDT | -0.775 | 0.30 | 31.9 | **100/0** | 199 | Reject (long-only) |

Source: [audit_top10_20260527.csv](audit_top10_20260527.csv), [audit_top10_20260527.md](audit_top10_20260527.md).

### Key structural findings
1. **Score model has near-zero discrimination above MIN_SCORE=92** — avg winner score ≈ avg loser score ≈ 96.9. The score gate filters noise out at the 92 threshold, but doesn't rank above it. Same in BNB forensics (98-100 anti-predictive).
2. **L/S imbalance is systemic** — 7/10 pairs are >75% one-sided. Strategy v2 scoring is structurally directional per asset.
3. **Live TON edge (PF 2.11, +0.35%/trade, n=22) is regime-dependent** — all 22 trades are shorts in a 16.6% downtrend. 95% CI on PF ≈ [0.95, 4.65]. Edge unproven against bull regime.
4. **BNB drop signal is confluent** — 3 independent reasons: negative backtest EV (-0.228R), L/S imbalance was 0 shorts in pre-prune backtest, score model anti-predictive on BNB (98-100 worse than 92-94).
5. **Hours 02-08 UTC cluster losses** across multiple pairs (visible in earlier TON+BNB sweep).

---

## SOAK-SAFE (executable during active soak)

### Task 1 — Build `confidence.py` (bootstrap CI tool) ✅ DONE 2026-05-27 (commit 4e06f5f)
**Goal:** Compute 95% bootstrap CI on PF and expectancy for any subset of live trades. Output: CLI tool that takes `--symbol --days --metric` and prints CI + recommended N for ±0.10R confidence.
**Why:** Decision gate (≥+0.05R → Phase 1) is currently asked of n=22 TON sample; SE ≈ 0.23R → can't reject "no edge". Need this tool to make defensible Phase 0 → Phase 1 call.
**Delivered:** Percentile bootstrap on expectancy, log-PF transform for heavy-tailed PF; one-sided LB at requested confidence; chi-square CI on σ → n_needed range via Wilson-Hilferty; N<5 refused; uniform JSON schema across all paths; stdlib only. Reviewed by 2 independent agents x 3 rounds, final 9.5/10 from both (code quality + statistical rigor angles).

### Task 2 — Build `robustness.py` (Q1-Q4 pre-deploy checker) ✅ DONE 2026-05-27
**Goal:** Single CLI that runs the 4 robustness gates on any candidate pair.
**Delivered:** PASS/FAIL/INCONCLUSIVE/GRAY per check + overall verdict. Q1 with configurable `--q1-threshold` (default 0.0, recommend 0.05 for live gate alignment). Q2 three-tier ρ thresholds (FAIL<0, GRAY [0,0.3), PASS≥0.3). Q3 ratio ≥ 0.20 with min-20-trades guard. Q4 multi-reference correlation with max-aggregation (PASS<0.6, GRAY [0.6,0.7), FAIL≥0.7). Imports config from `bot.py` (with explicit error reason if drift). Subprocess invocation with UTF-8 + timeout. `--csv-override` for testing. 9.6/10 from code-quality reviewer, 8.5/10 methodology (with documented known limitations).

### Task 3 — Build `correlation_matrix.py`
**Goal:** 30d log-return Pearson correlation matrix for any list of symbols. Output: heatmap CSV + flag pairs > 0.70.
**Why:** Methodology requires this for the greedy selection. BTC/ETH/SOL typically correlate > 0.75 — picking two of them gives no diversification.
**Acceptance:** Run on full top-10 + TON. Produces matrix. Identifies redundant clusters.
**Effort:** S (~3h).

### Task 4 — Score-bucket monotonicity analyzer
**Goal:** Per-pair: bucket trades by score (92-93, 94-95, 96-97, 98-100), compute expectancy per bucket, Spearman ρ(score, expectancy). Output: table + ρ values.
**Why:** BNB forensics showed score=100 worse than score=92 on BNB. Audition showed near-zero discrimination across pairs. Need to verify and quantify before using score-based filters per pair.
**Acceptance:** Run on TON, BNB, ZEC, HYPE, DOGE from existing backtest CSVs. Output Markdown table.
**Effort:** S (~3h).

### Task 5 — Extend `backtest.py` with `--block_hours` and `--min_atr_pct` flags
**Goal:** Add CLI flags to skip trades by UTC hour and minimum ATR percentage. **Offline tool only — does not touch live bot.**
**Why:** Need this to test the time-of-day blackout (02-08 UTC) and ATR-band hypotheses from earlier analysis.
**Acceptance:** Backtest TON+BNB on 30d with `--block_hours 2,3,4,5,6,7,8`; compare expectancy to baseline. Backtest TON with `--min_atr_pct 0.20`.
**Effort:** S (~4h).
**Pitfalls:** flag interaction with existing session logic, what's the right metric to compare (expectancy_R, PF, or both).

### Task 6 — `audit_pair.py` v2 with Selection Methodology integration
**Goal:** Extend `audit_pair.py` to compute the Edge Confidence Score (ECS) from methodology.md and apply the greedy selection algorithm. Output: per-pair ECS + final picks JSON.
**Why:** Current audition produces raw stats but doesn't apply the methodology consistently. Want a single source of truth that says "deploy X, Y" given fresh data.
**Acceptance:** Run on the top-10 audition data — reproduces "ZEC > HYPE > DOGE" ranking with explicit ECS values. Outputs `bot.py` config snippets ready to paste.
**Effort:** M (~10h).

### Task 7 — Walk-forward audition on next 30 days
**Goal:** When live calendar reaches 2026-06-26 (30d from today), re-run the top-10 audition on a fresh 30d window. Compare to today's results. Save delta report.
**Why:** Q1 in robustness checks requires two non-overlapping windows. Today's audition is window 1. Need window 2 for OOS validation.
**Acceptance:** New CSV + delta report. Pairs whose expectancy moved by > 0.15R between windows flagged as unstable.
**Effort:** S (run only — auditioning is automated by Task 6).
**Schedule:** Run on 2026-06-26.

---

## POST-SOAK (gated on Phase 0 decision)

Pre-condition: 50 live trades collected OR explicit user "soak off" signal.

### Task 8 — Apply `confidence.py` to live TON sample
**Goal:** Compute the true 95% CI on TON live expectancy. Decision: if CI lower bound > +0.05R → Phase 1; if straddles 0 → wait for more trades; if upper bound < +0.05R → Phase 0.5 rework.
**Output:** Memo with explicit decision.
**Effort:** S (run only).

### Task 9 — BNB drop (or longs-only re-tune)
**Default action: drop BNB from `SYMBOLS`.** Three independent reasons in forensics report.
**Alternative if user wants to keep:** `PAIR_MIN_SCORE['BNBUSDT'] = 95` AND assert longs-only.
**File:** [bot.py:62-74](bot.py)
**Effort:** S (~2h with review cycle).
**Pitfalls:** active BNB position handling (drain before removing? force-close?), DB references to BNBUSDT in views/reports.

### Task 10 — Add ZECUSDT to production (highest-confidence candidate)
**Pre-req:** Tasks 2-4 pass for ZEC (Q1-Q4 robustness, monotonicity check).
**Action:** Append `ZECUSDT` to `SYMBOLS`, set `PAIR_TP_R['ZECUSDT'] = 2.0`, `PAIR_MIN_SCORE['ZECUSDT'] = 92` (or audition-recommended).
**Soak:** 14 days with no further changes. Monitor live expectancy vs backtest +0.294R — if live diverges by more than 0.20R, roll back.
**Effort:** S (~2h with review cycle).
**Pitfalls:** ZEC liquidity on Bitget (verify min trade size, funding rate normality), webhook routing config, TradingView alerts.

### Task 11 — Add HYPEUSDT (conditional on DD acceptance)
**Pre-req:** Q1-Q4 pass for HYPE. **Caveat:** backtest DD=30% (vs ZEC 17%) — at 7× leverage on $100 capital this is real risk.
**Decision rule:** Deploy HYPE only if ZEC has held +EV in live for 14 days AND HYPE-vs-ZEC return correlation < 0.70.
**Effort:** S (~2h with review cycle).
**Pitfalls:** HYPE is younger asset — listing date check, may have shorter funding history.

### Task 12 — Time-of-day blackout (02-08 UTC)
**Pre-req:** Task 5 confirmed +EV uplift on TWO non-overlapping windows.
**Action:** Add `BLOCKED_HOURS_UTC = {2,3,4,5,6,7,8}` to bot.py config; gate in webhook handler before scoring.
**Effort:** S (~3h with review).
**Pitfalls:** session boundary edge cases, DST does not apply (UTC fixed), interaction with existing session pause logic in bot.py.

### Task 13 — Score model recalibration (large, deferred)
**Goal:** Strategy v2 scoring above MIN_SCORE=92 has near-zero discrimination. Either:
  (a) Lower MIN_SCORE to capture more sample and re-tune,
  (b) Build a strategy v3 that separates real-edge signals from noise above 92.
**Pre-req:** Phase 1 entered. Do NOT touch v2 scoring while in Phase 0.
**Effort:** L (multi-week — proper feature engineering + walk-forward).

### Task 14 — Drop TON if regime check fails
**Trigger:** If walk-forward audition (Task 7) shows TON expectancy < +0.05R on the second window.
**Action:** Demote TON from production to candidate pool; re-run audition for replacement.
**Why:** TON's 22-trade live edge may be regime-locked to current downtrend. The honest action if it breaks is to drop, not to retune.

---

## Order of execution

**Right now (during soak):** Tasks 1 → 2 → 3 → 4 → 5 → 6 in that order. They build on each other.
- Tasks 1-4 are independent analytics tools; 5 needs nothing else; 6 depends on 1-4.

**At 2026-06-26 (walk-forward window opens):** Task 7.

**At end of Phase 0 (soak off):** Tasks 8 → 9 → 10 → (14-day soak on ZEC alone) → 11 → 12.

**Deferred:** Task 13.

**Conditional:** Task 14.

---

## What NOT to do (anti-patterns the analysis surfaced)

- Don't raise MIN_SCORE to 95 or 100 globally — audition + BNB forensics both show higher scores are not better and sometimes worse.
- Don't add new pairs without Q1-Q4 robustness checks. Audition CSV is an upper bound (no slippage, no live gates).
- Don't deploy ZEC + HYPE simultaneously — soak them sequentially so impact attribution is clean.
- Don't enable `ENABLE_FUNDING_FILTER` yet — no evidence funding correlates with current losers; +0.05R hypothesis is speculative.
- Don't change TP_R from 2.0 without a multi-TP sweep on the candidate pair. Single-point picks overfit.

---

## References

- [audit_top10_20260527.csv](audit_top10_20260527.csv) — raw audition data
- [audit_top10_20260527.md](audit_top10_20260527.md) — audition narrative
- [ROADMAP.md](ROADMAP.md) — phase definitions
- [SYSTEM_STATE.md](SYSTEM_STATE.md) — current state
- [audit_pair.py](audit_pair.py) — existing audition pipeline
- [bot.py](bot.py) — main strategy (lines 62-74: pair config)
- [db.py](db.py) — schema
