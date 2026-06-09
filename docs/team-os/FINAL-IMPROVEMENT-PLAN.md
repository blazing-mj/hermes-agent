# TeamOS — Final Improvement Plan
*2026-06-09 · written by the outside reviewer (Claude) · grounded in live-system investigation*

## Where we are
The spine (Cortex→CTO→Worker→Validator→human gate) is **built and proven once end-to-end** (AGENTS-188).
All safety rails are live: integrity tripwire, 3-layer delete guards (file_tools / terminal / disk-cleanup),
webhook approval (no polling), circuit breakers, blocked capabilities, cross-model validation.
Gaps: no Integrator (last mile is freelanced), no metrics (no growth loop), no value ranking,
concurrency=1, backlog is 100% self-referential engine work.

## Open debt found in investigation
- **Bill/trader still on Anthropic API** (AGENTS-185 migration prepped, never applied — needs a fresh gated run)
- **Default bot unlocked** (`allowed_chats: ''`) — lock to MJ's chat id
- **36 git worktrees** accumulated — prune policy needed
- Dirty checkout files (AGENTS-215), spawn reliability (AGENTS-211), wall-clock fix unproven across restart

---

## Phase 0 — STABILIZE (now)
Goal: nothing latent can kill an autonomous run.
1. Wall-clock guillotine: config `gateway_wall_clock_timeout: 0` must be authoritative and survive restart. **Proof: a >10-min turn survives after a fresh gateway restart.**
2. AGENTS-211 spawn reliability: worktree pre-created atomically before task is "ready"; spawn-failures count toward the circuit breaker (no retry storms). TDD.
3. Hygiene: AGENTS-215 dirty files; prune stale worktrees (keep last N + active); lock default bot `allowed_chats` to MJ.
4. Finish AGENTS-206 (EMA ledger wiring) through the spine.

## Phase 1 — LAST MILE: Integrator + gate-redefine
Goal: validated work lands without MJ reading code; MJ gates only irreversible actions.
1. **Integrator stage** (deterministic code, not an agent): after Validator PASS —
   - reversible work → auto merge to main + deploy/reload + rollback artifact (git revert + restart commands recorded on the ticket)
   - irreversible (money/trading, live sends, production, customer, credentials, deletes, restarts) → stop at Needs-MJ with plain-language card
2. **Plain-language gate card** (template): Problem → What changed → How it behaves now → What MJ is approving → What MJ is NOT approving. No code.
3. Training wheels: first 3–5 auto-lands also send an FYI ping (not approval); then silent.
4. "Deployed + validated live" is the Done bar (no more committed-but-not-live).

## Phase 2 — AUTONOMY GATE (the milestone that matters)
Goal: prove the loop runs real work without a human saving it.
1. Load **5–10 real tickets** (EMA/OpenClaw fixes, ops debt, audits — NOT engine work).
2. Bar: **5 consecutive spine runs with zero manual interventions** (no hand-mkdir, no manual promote, no freelance merge).
3. Every failure during the gate = a fix ticket through the spine itself. No new capabilities until passed.
4. Re-run the **Bill provider migration (AGENTS-185 successor)** through the full spine as one of the gated tickets — closes the "no API" exception properly.

## Phase 3 — SCALE-OUT: pipelining + parallel fan-out  *(article: topology 3, parallelization pattern)*
Goal: kill "everything is slow."
1. Multi-ticket pipelining: lane watchers pull the next card whenever their stage is free — N tickets in flight at different stages.
2. Parallel fan-out via native `delegate_task` batch (exists, unused) for genuinely independent work:
   audits across files/sessions, multiple validation lenses, concurrent independent tickets.
3. **Output contracts before fan-out** (article): every fanned agent returns a fixed JSON schema; merge logic defined first.
4. Concurrency cap + per-branch circuit breaker (article: cascading-failure prevention).

## Phase 4 — GROWTH LOOP: the system improves itself  *(article: evaluator-optimizer at system level)*
Goal: "autonomously growing" becomes literal.
1. **Run scorecard** (`verify_spine_run`): every spine run auto-emits stages-passed, proof artifacts, interventions, duration, bounces — attached to the ticket. (Reviewer's manual checks, codified.)
2. **Metrics**: cycle time, bounce rate, human-interventions/ticket, %auto-landed, tickets/week.
3. **Reporter** publishes weekly digest; **worst metric auto-files an improvement ticket** → the spine fixes itself.
4. **Value/urgency ranking** in the classifier (extend failure_cost): the loop works on what matters first, not FIFO.

## Phase 5 — HARDENING, AS-DEMANDED ONLY  *(article: Module 5 failure modes)*
Build only when a real failure demands it:
- Schema validation at every substrate write (anti context-poisoning)
- Debate pattern (find→refute→judge) for money/prod tickets above the single cross-model validator
- Phase 6 early-clarification / Blocked lane (AGENTS-202)
- Hierarchical fan-out (3–7 agents per layer) only if flat hits limits

## Article borrowings — mapping
| Article concept | Where it lands |
|---|---|
| Runtime failures > agent failures | the whole plan's focus (coordination, not prompts) |
| Substrate-mediated comms | already ours (kanban/contracts/handoffs) — extend w/ schemas (P5) |
| Deps in code, not conversation | Integrator + deterministic DAG (P1), spawn fix (P0) |
| Circuit breakers / timeouts | P0.1, P0.2, P3.4 |
| Parallel fan-out + output contracts | P3 |
| Generator-verifier / debate | have cross-model validator; debate for money tickets (P5) |
| Governance: gate irreversible, prefer reversible | P1 gate-redefine |
| Evaluator-optimizer loop | P4 growth loop (system-level) |
| Keep orchestrator narrow | role-clarity: Cortex gates, CTO contracts, Worker wires, Validator proves, Integrator lands |
| 3–7 agents/layer, hierarchy beyond | P5 |

## Division of labor
- **Hermes (builder, owns live runtime):** all implementation/commits/deploys; P0–P4 builds.
- **Reviewer (Claude, read-only + artifacts):** specs (gate card, scorecard, metrics defs), independent verification of every phase, `verify_spine_run` tooling.
- **MJ:** drops real goals; approves irreversible only; reads digests. Never reads code.

## Standing rules (unchanged)
Subscription-only (Codex + Claude Max; no API except gated Bill exception until migrated).
Worktree-only execution. Money/trading/sends/production/credentials always human-gated.
Classifier fails closed. Done = deployed + validated live.
