# AGENTS-206 EMA Child-Output Ledger Observation Runbook

Status: additive, read-only observation path. No runtime wiring, daemon restart,
credential/config/provider edit, send, or OpenClaw write is required.

## Discover candidates

From the Hermes Agent checkout:

```bash
python scripts/ema_child_output_observe.py --limit 10
```

The helper only reads bounded candidates under `~/.openclaw`:
- EMA session JSONL from `~/.openclaw/agents/ema/sessions*`.
- Plausible EMA markdown artifact roots under the EMA workspace instance.
- Checkpoint JSONL is omitted from the candidate list.

## Observe after next normal live EMA test

After MJ/Operator runs a normal live EMA test, choose the fresh session JSONL and
artifact root from discovery output, then run:

```bash
python scripts/ema_child_output_observe.py \
  --session-jsonl /Users/alfred/.openclaw/agents/ema/sessions-archive/<session>.jsonl \
  --artifact-root /Users/alfred/.openclaw/workspace-agency/roles/email-strategist/instances/ema/<artifact-root>
```

The command embeds `scripts/ema_child_output_ledger.py` output under `ledger` and
reports `ledger_rows_found` under `ledger_observation_status` when rows are
found. `production_observation_status` remains
`pending_next_normal_live_EMA_test`; a human/operator must confirm the selected
session came from the next normal live EMA test before AGENTS-206 can move Done.

## Rollback

Remove these additive files from the Hermes Agent checkout:
- `scripts/ema_child_output_observe.py`
- `tests/scripts/test_ema_child_output_observe.py`
- `docs/team-os/AGENTS-206-ema-child-output-observation.md`

No OpenClaw files or daemons are changed, so no OpenClaw rollback is required.
