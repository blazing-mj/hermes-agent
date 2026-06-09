# Phase 2 — The Autonomy Gate
*Declared 2026-06-10 by the outside reviewer. Judge: `scripts/verify_spine_run.py` (fail-closed).*

## The bar
**5 consecutive spine runs on REAL tickets with a clean scorecard each:**
all stages present & correctly closed · validator = claude-max rail only · zero manual
interventions (no hand-mkdir, no manual promote, no freelance merge/land) · instability ≤ 2
failed runs per chain · validator verdict recorded · Integrator (not a human, not freelance)
lands the result. A single FAIL row = the run doesn't count; fix the cause via a spine ticket
and continue the streak from zero.

No new capabilities get built while the gate is open. Failures drive fixes; fixes flow
through the spine itself.

## Real-ticket menu (verified live on 2026-06-10, not guesses)
| # | Ticket | Evidence | Class |
|---|---|---|---|
| 1 | Fix 5 trader launchd daemons (position_monitor/resolver/learner/hunter/watchdog) | all status=2 since ≥May 30 | reversible infra → auto-land; NO live trading enable (that stays gated) |
| 2 | Remove dead aux providers (nous/openrouter) from chains | still in config; 46 log hits 8–9 Jun; pure waste | reversible config → auto-land |
| 3 | **Rotate + scrub OpenClaw secrets in git** (openclaw.json: ~10 token lines, tracked, auto-pushed to private GitHub; flagged 30 May, never done) | `git ls-files` confirms tracked; last sync 31 May | **credentials → HARD GATE (MJ)** |
| 4 | Bill/trader provider migration rerun (Anthropic API → Codex subscription) via full spine | billprinter config still `anthropic/claude-sonnet-4-6` | **money-adjacent → HARD GATE (MJ)**; includes compression pin (31 timeouts/7d, 9 fallbacks/24h) |
| 5 | AGENTS-186 Hermes slowdown / retry storms / over-working | already in Backlog | investigation → auto-land findings |
| 6 | EMA audit follow-ups (from the AGENTS-188 audit deliverable's fix list) | audit posted on AGENTS-188 | per-item classification |
| 7 | AGENTS-202 Phase 6 early-clarification / Blocked lane | queued by design | rail work → auto-land |

Recommended first five for the streak: **2 (easy), 5, 1, 6-pick, then 4 (gated, the graduation exam).**
Ticket 3 should run early regardless of the streak — it's a standing security exposure.

## Rules during the gate
- Producers file tickets to Backlog; Cortex triages; nobody skips stages.
- Every run gets `verify_spine_run.py <ticket>` attached to the Linear issue by the Integrator.
- Reviewer (Claude) independently re-runs the scorecard per run; disagreements = the run fails.
- Reports must cite DB run-counts, not impressions (188's "15 min lost" was 65 failed runs).

## After the gate
Phase 3 (pipelining + fan-out) unlocks. The streak proves the runtime; fan-out scales it.
