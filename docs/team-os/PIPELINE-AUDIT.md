# PIPELINE AUDIT — Backlog → Delivery, every stage, every outcome
*2026-06-10 · outside reviewer · grounded in live runs (AGENTS-5, 225, 188, 206), not design intent.*
*Legend: ✅ proven live · 🟡 built, unproven live · ❌ broken/missing*

## Stage 0 — PRODUCTION (a ticket is born)
**Triggers:** MJ (Telegram/Linear) · any agent · crons · Sentinel regression · watchdogs/monitors
**Outcomes:** ticket in Backlog of some project, with priority (default p3).
**Status:** ✅ all producer paths work.
**Gaps:** ticket quality varies (no template); projects outside scan list invisible (e.g. "Bill Printer Bot").

## Stage 1 — WAKE & PICK (the machine notices)
**Triggers:** (a) Linear issue-created webhook = doorbell ❌ *Linear delivery dead — zero deliveries reach us*;
(b) 30-min sweep ✅ *proven, 5+ clean ticks*; (c) any wake → full-backlog reconcile vs durable ledger ✅ *proven*.
**Outcomes:** ledger updated (add/remove) → exactly ONE top card picked (priority → oldest) under lease;
or no-op (busy / backlog empty).
**Gaps:** doorbell delivery (THE blocker, with continuation below); scan list is explicit (Hermes System +
OpenClaw Core only) — new projects must be added or are invisible.

## Stage 2 — TRIAGE (Cortex judges)  ← the flexibility stage
**Trigger:** the picked card.
**Outcomes (current):**
- (a) hard-gated surface (money/trading/sends/credentials/prod) → **Needs-MJ** + assignee + Telegram ping ✅ *proven (AGENTS-5 ping received)*
- (b) safe & clear → **spine chain created** (Cortex/CTO/Worker/Validator kanban cards) ✅ *proven (AGENTS-5, 7/7 genuine sessions)*
- (c) junk/duplicate → Canceled 🟡
**Outcomes (REQUIRED, per MJ — partially built):**
- (d) **unclear / out-of-capability → RESEARCH then ASK**: Cortex investigates read-only (codebase, docs,
  web if needed), then attaches a structured ask {type: info|access|decision · plan-so-far · blocker ·
  exactly-what's-needed · options} → card to **Blocked** lane → ping MJ. 🟡 *AGENTS-202 code exists
  (asks + Blocked lane, 13 tests) — NEVER proven live. Must be pulled into the live path.*
- (e) **capability gap → BUILD-CAPABILITY sub-tickets**: if delivery needs a new agent/profile/script/tool,
  triage decomposes into sub-tickets ("build X", then "do the task using X"), each classified on its own
  (new scripts = reversible/auto-land; new AGENT/profile = consequential → MJ gate). ❌ *missing concept —
  decomposer.py exists in team_os but is not wired into intake triage.*

## Stage 3 — CONTRACT (CTO)
**Trigger:** chain's CTO card. **Outcomes:** work order (files, denylist, proof, **pinned validator route**)
→ Worker card ready; or BOUNCE back to triage. **Status:** ✅ proven; validator pinning enforced by role
registry (Ruta-class improvisation now structurally rejected).

## Stage 4 — BUILD (Worker)
**Trigger:** ready Worker card (worktree pre-created atomically). **Outcomes:** implement in isolated
worktree → handoff JSON w/ claims+diff proof; or **blocked** (hit gated surface mid-build → correct stop,
proven on AGENTS-5); or crash → circuit breaker auto-blocks after consecutive failures ✅ (no more storms).

## Stage 5 — VALIDATE (independent)
**Trigger:** handoff exists. **Outcomes:** Claude-Max cold session (ONLY rail, registry-enforced) → PASS
w/ artifact; or BOUNCE → Worker retry/Todo. **Status:** ✅ proven. Grader v2 independently re-judges every
run (genuine sessions, stability, interventions).

## Stage 6 — GATE (the human edge)
**Trigger:** validated work classified.
**Outcomes:**
- reversible → proceed to Integrator (no MJ) 🟡 *never exercised live*
- irreversible/uncertain → **Needs-MJ** + dual-ping ✅ + plain-language gate card 🟡 *(template committed;
  intake-path rendering partial)*
- MJ moves to **Approved** → continuation requeued ❌ **DEAD — Linear delivery broken; formally proven
  (25-min watch, zero deliveries). FIX #1.** Decision-reconcile fallback in sweep = REQUIRED so a human
  decision can never strand again. ❌ **FIX #2.**
- MJ → Rejected (+comment) → stop/revise 🟡 unproven
- MJ comments a question → agent answers in Linear 🟡 unproven

## Stage 7 — LAND (Integrator)
**Trigger:** validator PASS + gate cleared. **Outcomes:** merge to main + deploy/reload (checkpoint+resume
protocol; never billprinter) + rollback recorded on ticket + FYI ping (first 5) → Done (deployed+validated bar).
**Status:** 🟡 wired into dispatcher, tests pass — **has NEVER fired live** (all past landings hand-pushed).

## Stage 8 — AFTER (the loop closes)
- Sentinel arms regression watch → regression auto-creates linked Backlog ticket ✅ proven once (forced test)
- Reporter digest 🟡 (code exists; needs schedule) · Metrics ✅ `team_os_metrics.py` (baseline: 62.7% bad-run
  rate, 13.5% inline rate — the numbers the growth loop must drive down)
- Worst-metric → auto-files improvement ticket ❌ Phase 4 proper.

## THE CRITICAL PATH (ordered)
1. **Linear delivery fix** + prove one real end-to-end event (unblocks doorbell + decisions)
2. **Decision-reconcile in sweep** (Approved/Rejected cards picked up even with dead webhook)
3. **First live Integrator auto-land** (AGENTS-5 continuation is the waiting test fixture)
4. **Wire AGENTS-202 asks/Blocked into the live intake path** (flexibility outcome d)
5. **Capability-bootstrap triage** (outcome e: build-new-agent/script sub-tickets via decomposer)
6. Reporter schedule + worst-metric auto-ticket (growth loop closes)

## Standing invariants (unchanged)
Fail-closed classifier · money/trading/sends/credentials/prod always gated · worktree-only builds ·
cross-model validation · events are doorbells, state from reconciliation · LLM reasons, deterministic code commits.
