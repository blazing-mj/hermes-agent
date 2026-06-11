# Master Ship Plan — 2026-06-11

Scope: (A) lifecycle curation autopilot — proposals apply themselves with no human approval; (B) the 9 In-Progress survivors from the 2026-06-11 board audit. Author: reviewer session. Status: awaiting MJ "go" before shipping.

---

## Track A — Lifecycle Autopilot

### Current architecture (verified 2026-06-11)

Pipeline: daemon/weekly trigger → curator (sonnet, librarian identity prompt) → **Layer 2 sentinel check** (required substrings verbatim, abort+alert on strip) → **density floor** (median-of-3 Haiku scores vs per-surface `quality_min`; retry on sonnet-4-6; quarantine below floor — 56% quarantine rate shows the floor works) → **peer review** for ≥0.92 surfaces → write `<file>.proposed.md` (live file untouched).

The only human step: `review.py accept`, which refuses protected paths (`AGENTS.md`, `SOUL.md`, …) without `--force-protected`. `auto_accept.py` already auto-promotes safe classes (DREAMS, BOOTSTRAP, daily logs, MEMORY.md via Haiku semantic judge).

### Diagnosis

The human approval is the **weakest** gate in the chain: the operator sees four byte counts and cannot evaluate semantic preservation better than the judge ensemble that already ran. The correct move is not "auto-approve" but **replace the human gate with machine gates that are strictly stronger**, plus rollback that is faster than a human could ever react.

Evidence the current gap is real: 3 of the 4 pending proposals are stale no-ops (proposed size == live size — generated against older bytes), 1 is an expansion. Auto-applying today's queue naively would clobber newer files with stale content.

### Design — seven additions, all in `~/.openclaw/lifecycle/` (no gateway/dist changes)

| # | Addition | What it does |
|---|---|---|
| A1 | **Provenance binding** | Proposal records source SHA-256+size at curation. Apply refuses on live-file mismatch → discard + regenerate. Kills the stale-proposal class. |
| A2 | **Apply-time sentinel re-check** | `check_sentinels()` re-runs at apply moment (today: generation-time only). Fail-closed. |
| A3 | **Apply-time semantic judge** (protected surfaces) | Extends the MEMORY.md judge: extract every imperative (MUST/NEVER/ALWAYS), tool name, path, numeric constraint from live file; verify present-or-equivalent in proposal; median-of-3 Haiku; floor = registry `quality_min`. Fail → quarantine. |
| A4 | **Atomic, hardlink-correct apply** | temp + `os.rename` for `st_nlink==1`; for hardlinked surfaces (protocols shared across 3 workspaces): replace canonical path, re-link siblings to the new inode, verify inode equality post-apply. Replaces `shutil.move`, which silently diverges hardlinks. |
| A5 | **Instant rollback** | Keep promotions archive; add `review.py rollback <file>` (one command, newest archive). Post-apply hook re-validates (sentinels + parse) and auto-rolls-back on failure. |
| A6 | **FYI, not gate** | Daily lifecycle digest line per applied trim: file, −%, density score, rollback path → Telegram. Same pattern as Integrator FYI. |
| A7 | **Staged rollout + kill switch** | Per-tier flag in `thresholds.yaml`. Tier 1: subagent instance AGENTS.md (smallest blast radius). Tier 2: role AGENTS.md + protocols. Tier 3: SOUL.md last (peer review already mandatory). Any auto-rollback event freezes the tier. |

Plus hygiene now (A8): discard the 4 stale pending proposals, regenerate fresh under the new pipeline.

### Test & rollout

Unit + fault-injection tests: stale hash, stripped sentinel, hardlink divergence, judge fail, mid-apply crash. Then a **24h dry-run mode** — pipeline logs apply/refuse decisions without touching files; compare against what a human would have approved before flipping Tier 1 live.

Gate: MJ "go" on this design. Everything is reversible, archived, kill-switched.

---

## Track B — the 9 survivors, ship order (risk-weighted)

| # | Ticket | Plan | Gate |
|---|---|---|---|
| B1 | **AGENTS-122** p3 | Config-only: move auxiliary tasks off `provider: auto` → subscription rails; drop dead API fallbacks (openrouter payment-fail, Nous no-auth, bad Copilot token). Verify via gateway logs: zero API auxiliary attempts. | none |
| B2 | **AGENTS-153** p1 | Surgical backport of upstream provider-error→terminal-failure into pinned 2026.5.7 dist (same style as codex bridge). Fault-injection: forced provider 404 in child run must yield FAILED child, not success/no-output. Dist `.bak` + smoke before/after. | none |
| B3 | **AGENTS-238** p2 | Integrator final hop: auto-Done when validator PASS + reversible + landing evidence (64e11d4ec gate) + human-language landed-summary per GATE-CARD-TEMPLATE. Backfill AGENTS-225/227/228/236 + AGENTS-5. Extends the 14-test webhook suite. | none |
| B4 | **AGENTS-243** p2 | Telegram inline Approve/Reject/Question on Needs-MJ pings. `callback_query` handler, signed (MJ chat_id only), idempotent, reuses the exact Linear-Approved decision path so buttons and board cannot diverge. Feature-flagged. | brief bot restart |
| B5 | **AGENTS-157** p3 | Root cause + local patch exist (attachment burst vs in-flight image turn). Review patch, add regression test, deploy bundled with B4's restart window. | same restart |
| B6 | **AGENTS-163** p1 | Diagnosis posted (ThreadPool turn, inactivity-only polling, no wall-clock guard on provider stream). Build: enforced per-turn budget (reuses AGENTS-216 authoritative wall-clock config), provider-call timeout/failover, stuck-turn auto-recovery, interface isolation from worker jobs. Fault-injection in isolated worktree per ticket constraints. | MJ PASS on diagnosis |
| B7 | **AGENTS-221** p2 | Containment now: gitignore + `git rm --cached` (runtime file preserved), config-watcher-safe. History rewrite (force-push) + key rotation = MJ-gated. | MJ for rewrite + keys |
| B8 | **AGENTS-96** p1 | Code shipped (fd29f0840), prevents future downgrades. Regression = expired/invalid refresh token — unfixable by code. MJ does one interactive `claude login` (Max); I verify Keychain metadata + 48h no-relogin. | MJ login |
| B9 | **AGENTS-176** p3 | Mostly done (new PAT live, scrubs done, venv aligned). Remaining: Bitwarden vault login + migration — I script it so MJ's part is minutes. | MJ login |

## Everything MJ must personally do (all else ships without him)

1. "Go" on Track A design → lifecycle becomes autonomous.
2. PASS on the AGENTS-163 diagnosis (his call per ticket).
3. AGENTS-221: approve history rewrite; mint replacement keys.
4. AGENTS-96: one `claude login` on Max.
5. AGENTS-176: one Bitwarden login session.
