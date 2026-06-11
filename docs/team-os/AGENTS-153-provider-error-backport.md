# AGENTS-153 — provider-error terminal-failure backport (OpenClaw 2026.5.7)

Surgical backport of upstream `openclaw/openclaw` commit `18f94fc83a72`
("fix(agents): classify embedded provider business denials for fallback",
PR #84814, 2026-05-30) into the pinned 2026.5.7 dist. No core upgrade.

## Bug

A child/sub-agent run whose payloads carried a provider error (404 stale
model, 401 auth, billing denial) was classified `null` (= success/no-output)
for every non-GPT5 model: `classifyEmbeddedPiRunResultForModelFallback`
collected `errorText` from `isError` payloads, then discarded it at the
`if (!isGpt5ModelId(params.model)) return null;` gate. Incident: EMA
vision sub-agent on stale `google/gemini-2.0-flash` 404'd; the parent saw a
successful empty child run.

## Patch

File: `/opt/homebrew/lib/node_modules/openclaw/dist/result-fallback-classifier-CrVa7J1V.js`
Backup: `result-fallback-classifier-CrVa7J1V.js.bak.before-AGENTS-153-20260611` (sibling, per dist convention)

1. Added import: `import { t as classifyFailoverReason } from "./errors-BqFqz2qx.js";`
2. Inserted before the `isGpt5ModelId` gate: when `errorText` is non-empty and
   `classifyFailoverReason(errorText, {provider})` returns `auth`,
   `auth_permanent`, `billing`, or `model_not_found`, return a terminal
   failure `{code: "embedded_error_payload", rawError}`.

**Documented divergence from upstream:** upstream's allow-set is only
`{auth, auth_permanent, billing}`. A bare stale-model 404 classifies as
`model_not_found`, which upstream still misses — we include it and map it to
reason `"format"` (a reason already used in this module), because the 404
class IS the AGENTS-153 incident.

## Proof (node smoke, 2026-06-11)

| case | input | result |
|---|---|---|
| 404 on non-GPT5 child | `isError` payload, model_not_found text | `embedded_error_payload` / format ✓ (was `null`) |
| 401 auth on non-GPT5 | invalid x-api-key text | `embedded_error_payload` / auth ✓ |
| clean non-GPT5 reply | normal payload | `null` ✓ unchanged |
| empty non-GPT5, no error | `[]` | `null` ✓ unchanged |
| GPT5 empty | `[]` | `empty_result` ✓ unchanged |
| tool-error text (exit code 1) | `isError` non-provider text | `null` ✓ — tool errors are not provider failovers |

## Live status / rollback

Gateway PID at patch time started 05:21 — patch is on disk, live at the next
gateway restart/cycle. Rollback: copy the `.bak.before-AGENTS-153-20260611`
sibling back over the `.js` and restart the gateway.
