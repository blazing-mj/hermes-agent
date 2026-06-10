# SYSTEM MAP — where everything lives
*For the Team OS CTO and any agent that needs the structure. 2026-06-10.*

Two systems on one Mac mini. Hermes = the dev-team/orchestration stack (subscription-only).
OpenClaw = the agency fleet (EMA etc.). Team OS lives in Hermes and acts on both.

## 1. HERMES — code (the repo everything Team OS ships into)
`/Users/alfred/.hermes/hermes-agent/` — git main checkout (push remote: fork/main)
- `hermes_cli/team_os/` — THE Team OS engine: `cortex.py` (gate), `dispatcher.py` (+ Integrator call),
  `integrator.py`, `role_registry.py` (who may do what), `linear_webhook.py` (MJ decisions + doorbell),
  `thin_loop.py` (contract→worker→validator slice), `failure_cost.py` (classifier), `contracts.py`, `approvals.py`
- `hermes_cli/kanban_db.py` — kanban task/run/lease engine (SQLite)
- `gateway/` — the always-on agent runtime: `run.py` (turn loop, restart/resume), `resume_state.py`
  (checkpoint protocol), `platforms/` (telegram, slack, webhook receivers)
- `tools/` — agent tools: `delegate_tool.py` (subagent spawning), `terminal_tool.py` + `file_tools.py`
  (both carry tracked-file delete guards), `environments/`
- `plugins/disk-cleanup/` — disk cleanup (now refuses git-tracked files)
- `scripts/` — ops tooling: `verify_spine_run.py` (autonomy-gate grader), `verify_board_flow.py`,
  `worktree_integrity_check.py` (tripwire), `team_os_lane_watcher.py`, `restricted_linear_writer.py`
  (gated Linear board mover), `ema_child_output_ledger.py` (EMA truth-checker)
- `docs/team-os/` — canon: FINAL-IMPROVEMENT-PLAN.md, PHASE2-AUTONOMY-GATE.md,
  board-transitions.json (lane allowlist), GATE-CARD-TEMPLATE.md, this file
- `tests/` — pytest tree mirroring the above

## 2. HERMES — runtime home (state, not code)
`/Users/alfred/.hermes/`
- `config.yaml` — default agent config · `profiles/{cortex,cto,ruta,billprinter}/` — per-agent homes
  (SOUL.md, config.yaml, .env, logs/, state/, memories/) — all run the SAME repo code above
- `kanban/boards/{hermes-system,openclaw-core,...}/kanban.db` — execution queues (SQLite)
- `worktrees/` — isolated git worktrees where Workers build (throwaway)
- `scripts/` — symlinks into repo scripts (live == committed) · `bin/` — launchers (`claude-max-code`
  = Claude Max shell-out rail, `linear-agent` = Linear CLI)
- `state/`, `logs/` — runtime state incl. resume checkpoints, outboxes, ledgers
- `trader/` — Polymarket trader data (Bill). NEVER auto-touched.

## 3. Launchd (what actually runs)
`~/Library/LaunchAgents/ai.hermes.gateway[-profile].plist` — 5 gateways (default, cortex, cto, ruta,
billprinter) all exec `hermes-agent/venv/bin/python -m hermes_cli.main --profile X gateway run`
- `ai.hermes.gateway.watchdog` — health backstop · `ai.hermes.worktree-integrity` — 5-min file tripwire
- `ai.hermes.cloudflared.mission-control` — HTTPS tunnel: Linear webhooks →
  `mission-control.agentjuice.ai/webhooks/linear-team-os` → gateway webhook → `linear_webhook.py`

## 4. OPENCLAW — two very different locations
**Platform code (NOT in any of our repos):** `/opt/homebrew/lib/node_modules/openclaw/`
- global npm package, LOCALLY PATCHED `dist/` (patch log in workspace STATUS-BOARD)
- Slack integration code: `dist/extensions/slack` + `node_modules/@slack` + `skills/slack`
- If this machine dies: reinstall npm package + re-apply patches from STATUS-BOARD notes.

**Config + workspaces (git repo → github.com/blazing-mj/openclaw-config, private):** `/Users/alfred/.openclaw/`
- `agents/ema` + 13 `agents/ema-*` sub-workers (homes/configs/sessions)
- `workspace-agency/` (agency brain, 800+ files), `workspace-ghost/`, `workspace/`, `workspace-principal/`
- `lifecycle/` — custom launchd daemons (com.openclaw.*) · `scripts/`, `bin/`, `tests/`, `docs/`
- `openclaw.json` — channels.slack + plugins.entries.slack config (⚠ holds tokens; rotation/scrub ticket pending)
- Auto-syncs to GitHub. Contains NO platform code — config/workspaces/glue only.

## 5. Cloud surfaces
- **Linear** (cloud) — the human-visible board; Team OS reads/writes via `bin/linear-agent` through
  `restricted_linear_writer.py` (transition allowlist) and receives events via the webhook above.
- **GitHub** — `blazing-mj/openclaw-config` (OpenClaw config/workspaces) · hermes-agent fork (code).
- **Subscriptions** — OpenAI Codex (ChatGPT OAuth) drives gateways; Claude Max via `claude` CLI
  shell-out = the independent validator rail. No API keys in the execution path (one exception:
  billprinter still on Anthropic API — migration queued as gated AGENTS-224).

## Rule of thumb
Code → `~/.hermes/hermes-agent` (git). State → `~/.hermes`. Agency brains → `~/.openclaw` (git).
Agency engine → npm package (patched, unversioned). Boards → Linear (cloud) + kanban SQLite (local).
If it's not committed in one of the two git repos, assume it can vanish.
