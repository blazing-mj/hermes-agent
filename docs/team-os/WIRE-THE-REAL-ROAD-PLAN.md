# Wire the Real Road — TeamOS from façade to real agent pipeline

The agents (Cortex, CTO, Worker, Validator) all EXIST as real, well-designed
agents. The live flow currently runs keyword/template/marker STAND-INS instead
of calling them. This plan replaces each stand-in with its real agent, one at a
time, each independently testable, **with TeamOS staying PAUSED the whole time**
(wiring is dormant until the separate turn-on step).

## The gap today (verified)

| Stage | Real agent exists? | Live flow runs… |
|---|---|---|
| Cortex (brain/audit) | YES — `profiles/cortex/SOUL.md`, own gateway | a keyword classifier (`_is_gated`) wearing Cortex's name; no LLM, no audit, no questions |
| CTO (contract/scope) | partial — `contracts.py` template | a template fill; no real scoping, doesn't spawn worker |
| Worker (build) | YES — `worker_runner.py` (claude-max-code, isolated worktree) | nothing — never invoked (`dry_run=True`, no hook) |
| Validator (cruel review) | YES — `validator_runner.py` (cross-model, 5-step) | nothing — never invoked |

Doorbell → `cortex_work_intake_dispatch.sh` → `exec python3 team_os_linear_intake_motor.py` (keyword motor under Cortex's home). The real `run_cortex`/`run_worker`/`run_validator` are never called in the live path.

## Stages (each: built paused, tested in isolation, nothing live until turn-on)

### Stage A — Wake the real Cortex (the brain) ← START HERE
Replace the keyword `_is_gated` decision with a real Cortex LLM call (as the
cortex profile) that:
- **Audits**: is this real / worth doing / already done / a duplicate? What's the root need?
- **Classifies** with reasoning: system, severity, root cause, confidence, reversible vs gated.
- **Asks questions** in Linear when the ticket is vague/uncertain (the clarification loop that's missing today).
- **Emits a real grounding doc** (`team_os.grounding.v1`) + thin contract, then bounces to CTO.
- Keyword classifier kept as a **fail-safe fallback** only if the LLM call errors.
Test: run it (dry, paused) on 5–10 real Backlog tickets; confirm it audits, asks sensible questions, and classifies correctly vs the old keyword verdict. No board writes.

### Stage B — Real CTO (the contract)
Cortex bounces to CTO. CTO turns the grounding doc into a real contract:
files_to_touch, acceptance criteria, bounce conditions, reversibility. Posts it
to Linear; optionally confirms scope with MJ before the worker starts.
Test: grounding doc in → concrete contract out; scope matches the ticket.

### Stage C — Connect the Worker (the engine)
Wire CTO → spawn the real Worker (`run_worker`, already built: isolated git
worktree, claude-max-code, proof commands, handoff.json with real commit).
Activate dispatch for safe/low-cost tickets only.
Test: one real safe ticket produces a real commit + handoff, in a worktree, nothing landed.

### Stage D — Connect the Validator (cruel, cross-model)
After the worker, run the real Validator (different model, 5-step adversarial,
PASS/BOUNCE, max-3 bounce loop → escalate to MJ).
Test: feed it a deliberately-bad handoff → it BOUNCEs; a good one → PASS.

### Stage E — Turn-on (the LAST step, MJ-directed)
Real road wired + each stage tested. Then: deploy (gateway restart) → lift the
kill-switch → one supervised live ticket watched end to end (doorbell → real
Cortex audit → CTO contract → Worker build → Validator → Integrator land →
Done, with the Telegram buttons).

## Invariants throughout
- Kill-switch stays ON until Stage E. Every stage is built/tested while paused.
- Hard gates unchanged: money/credentials/production/sends always → MJ.
- Each stage independently testable; no stage depends on turn-on to verify.
- Real agents already exist — this is wiring + prompting, not green-field.
