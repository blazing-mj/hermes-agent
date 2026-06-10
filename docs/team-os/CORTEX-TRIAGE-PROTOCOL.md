# CORTEX TRIAGE PROTOCOL v1 — what happens to EVERY picked ticket
*2026-06-10 · canon, per MJ's direct specification. Implements PIPELINE-AUDIT stage 2 to the max.*

The intake motor picks ONE top card (priority → oldest). Cortex then runs THIS protocol —
a real Cortex agent session per ticket, not a keyword script. Every step leaves an artifact
on the ticket. One ticket at a time, start to finish.

## Step 1 — AUDIT (is this even still real?)
Read the ticket, its comments, related code/docs/sessions. Decide:
- **RELEVANT** → continue to Step 2
- **STALE / superseded** → comment one-paragraph reason + move to Canceled (reversible board
  action; if any doubt → ask MJ instead, Step 4)
- **DUPLICATE** → link the original, Canceled
- **ALREADY DONE** → cite the proof (commit/artifact), propose Done via Needs-MJ (closing
  someone's ticket is a human-gate action)

## Step 2 — REWRITE (dual brief, attached as a comment)
**Human brief (for MJ, zero jargon):** what this is · why it matters · what will visibly
change · risks in plain words. ≤8 lines.
**Agent brief (for the spine):** grounding (file:line refs) · exact scope · non-goals ·
denied surfaces · proof requirements · validator route. This becomes the CTO contract input.

## Step 3 — SIZE & SPLIT
If the work exceeds one worker slice (≥~half-day, or >1 surface, or mixed risk classes):
create **sub-issues** (mini-tasks) with parent link + dependency order + per-sub
classification. Parent becomes a tracking card; subs flow the spine individually.

## Step 4 — NEEDS (the flexibility gate; any can fire, all ping MJ once)
- **Missing info/decision** → structured ask on the ticket:
  `{type: question|decision, plan-so-far, blocker, exactly-what-MJ-must-answer, options}`
  → **Blocked** lane → Telegram + Linear ping. MJ's answer (webhook) resumes Step 5.
- **Missing access/permission/tool** → access request ask:
  `{type: access, tool/credential/API needed, why, scope requested, what happens without it}`
  → Blocked + ping. NEVER self-provision credentials or accounts.
- **Missing capability** (no agent/script/profile exists that can do this) → create
  `build-capability` sub-issue(s) FIRST (new scripts = reversible; new AGENT/profile or
  external account = consequential → Needs-MJ), then the original task depends on them.
- **Research needed** → read-only research pass (codebase, docs, web read-only) BEFORE
  asking; attach findings so MJ answers an informed question, not a lazy one.

## Step 5 — CLASSIFY & ROUTE (tuned, not keyword-panic)
- **Hard gate, always:** money/trading · live sends (email/Klaviyo/social) · credentials ·
  production/customer surfaces · deleting data · new external accounts.
- **Proceed WITHOUT MJ:** reversible code/tests/docs/rail work in a worktree touching no
  denied surface — even if the ticket *mentions* a gated word, what matters is what the
  WORK TOUCHES (e.g. "fix trader daemons' launchd plists, no restart" = reversible infra;
  "investigate X" read-only = proceed). Keyword-only gating is a bug, not safety.
- **Uncertain → Needs-MJ** (fail-closed survives, but with Step 1-4 done, "uncertain"
  should be rare — an uncertain card must carry the dual brief + a specific question).

## Step 6 — EXECUTE
Spine chain (CTO contract from the agent brief → Worker → Validator → Integrator/gate),
one by one. The dual brief travels with the chain. Grader v3 judges every run.

## Invariants
- One artifact per step, on the ticket — auditable later.
- MJ is pinged ONCE per need, with a specific ask — never "look at this."
- A ticket may loop Steps 4→5 after MJ answers, but never skips the audit or the brief.
- This protocol runs as a Cortex AGENT session (LLM judgment for audit/rewrite/asks);
  routing/board moves stay deterministic code.
