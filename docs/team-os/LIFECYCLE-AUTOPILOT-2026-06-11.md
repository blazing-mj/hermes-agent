# OpenClaw Lifecycle Autopilot — shipped 2026-06-11

MJ directive: curation proposals (AGENTS.md/SOUL.md trims) must apply
systematically without his approval. Design: replace the human gate with
machine gates that are strictly stronger, plus rollback faster than a human.

## What shipped (all in `~/.openclaw/lifecycle/`)

| piece | file | what it does |
|---|---|---|
| A1 provenance | `promotion.py` | orchestrator writes `<f>.proposed.meta.json` (sha256+size of exact source bytes); apply refuses + discards when the live file changed since curation |
| A2 sentinels at apply | `promotion.py` | `protection.check_sentinels()` re-runs at apply moment, fail-closed |
| A3 judge ensemble | `autopilot.py` | majority of 3 independent Haiku integrity votes (every MUST/NEVER, tool name, path, status marker, threshold must survive); verdicts cached by content hash so watcher ticks don't re-spend |
| A4 atomic + hardlink-correct | `promotion.py` | tmp+fsync+rename; hardlink siblings re-linked to the new inode and inode-verified (old `shutil.move` silently diverged the 3 workspaces) |
| A5 rollback | `promotion.py`, `review.py rollback <live>` | archive-first; post-apply validation auto-restores on mismatch; one-command manual restore |
| A7 tiers + freeze | `thresholds.yaml autopilot:`, `state/lifecycle-autopilot.json` | tier1 (subagent AGENTS.md) enabled, tier2 (role AGENTS/protocols/SKILL) and tier3 (SOUL.md) off; any auto-rollback freezes its tier |
| A8 queue hygiene | orchestrator fix | **root bug found**: quarantined proposals were never removed — they lingered as pending (that's what MJ's 4 stale pendings were). Quarantine now moves files to `archive/lifecycle-2.4/quarantine/`; the 4 stale pendings discarded, queue empty |

`review.py accept` (manual) and the autopilot share the same `safe_apply`
engine; manual keeps the Layer-1 protected gate and tolerates legacy
sidecar-less proposals, autopilot requires provenance.

## Proof

- 23 fault-injection tests green (`lifecycle/tests/test_promotion.py`):
  stale hash, missing provenance, quarantined outcome, sentinel strip,
  dry-run no-touch, hardlink inode equality, rollback, frozen tier, judge
  reject/approve paths.
- Live dry-run smoke: synthetic tier1 proposal flowed config→tier→provenance
  →sentinels→dry-run verdict; live file untouched; events in lifecycle.jsonl.
- Real judge discrimination: faithful trim → SAFE; lossy trim → UNSAFE
  naming the dropped directives.

## Rollout state

`autopilot.enabled: true`, `dry_run: true` (24h observation window),
tier1 only. Watcher (`ai.openclaw.lifecycle-auto-accept-watcher`) picks the
new code up automatically — fresh process per tick, no restart needed.
Flip to live: set `dry_run: false` in `lifecycle/thresholds.yaml` after the
dry-run log review; then enable tier2, then tier3.
