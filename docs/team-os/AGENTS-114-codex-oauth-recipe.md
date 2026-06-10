# AGENTS-114 — Codex OAuth on pinned 2026.5.7: the surgical recipe (verified groundwork)
Status: ready to execute. No core upgrade needed — MJ's cherry-pick call validated.

## Verified facts
1. Latest @openclaw/codex plugin HAS the ChatGPT OAuth flow; 2026.5.7-era plugin does NOT (API-key only — confirmed live).
2. Latest plugin fails to load on 2026.5.7 for ONE reason: it requires `dist/plugin-sdk/root-alias.cjs/<module>` (directory layout); local has `root-alias.cjs` as a FILE (Proxy-based monolithic SDK re-exporter, exports at L408).
3. The underlying runtimes EXIST locally: dist/exec-approval-BkpBA7ht.js, exec-approval-forwarder.runtime-*.js etc.

## Execution steps (next session)
1. `openclaw plugins install @openclaw/codex --force` (latest) — then enumerate ALL its requires:
   `grep -rhoE "plugin-sdk/root-alias\.cjs/[a-z-]+" ~/.openclaw/npm/node_modules/@openclaw/codex/dist/*.js | sort -u`
2. Convert root-alias.cjs file → directory: `index.js` = original file (require-dir resolution keeps old consumers working); add `package.json {"main":"index.js"}` for safety.
3. For each required subpath X: create `root-alias.cjs/X.js` aliasing the matching local plugin-sdk module or dist chunk (verify exports match what the plugin destructures — grep plugin usage per X).
4. Restart gateway → confirm plugin loads (no module error) → `openclaw models auth login --provider openai` in tmux (TTY) → OAuth flow should appear → `open <URL>` pops the window for MJ (or device code).
5. Flip staged primaries (AGENTS-114-staged-routing.json) → smoke a copywriter task on openai/gpt-5.5 → close 114.
Rollback: restore root-alias.cjs file from backup; plugin --force back to @2026.5.7.

## 153 (same surgical style)
Upstream source diff 2026.5.7→2026.6.5 for provider-failure terminal-state handling → port into dist chunks like prior STATUS-BOARD backports → before/after test: provider error → child run FAILS (not silent success).
