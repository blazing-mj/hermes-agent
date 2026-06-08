#!/usr/bin/env bash
set -euo pipefail
LOG="/Users/alfred/.hermes/logs/agents199-live-deploy.log"
exec >>"$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S %z'; }
UID_NOW="$(id -u)"
ROOT="/Users/alfred/.hermes/hermes-agent"
CATCH_PLIST="/Users/alfred/Library/LaunchAgents/ai.hermes.catch-deleter.agents199.plist"
CATCH_LOG="/Users/alfred/.hermes/logs/catch-deleter.launchd.log"
CATCH_ERR="/Users/alfred/.hermes/logs/catch-deleter.launchd.err.log"
LABELS=(ai.hermes.gateway ai.hermes.gateway-billprinter ai.hermes.gateway-cortex ai.hermes.gateway-cto ai.hermes.gateway-ruta)

echo "===== AGENTS-199 LIVE DEPLOY START $(ts) ====="
cd "$ROOT"
echo "commit=$(git rev-parse --short=9 HEAD)"
echo "guard_symbol=$(grep -n '_tracked_file_delete_guard' tools/terminal_tool.py | head -1)"
echo "pre_pids:"
for label in "${LABELS[@]}"; do
  launchctl print "gui/${UID_NOW}/${label}" 2>/dev/null | awk -v label="$label" '/pid = / {print label" pid=" $3}' || true
done

cat >"$CATCH_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.hermes.catch-deleter.agents199</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3.13</string>
    <string>/Users/alfred/.hermes/hermes-agent/scripts/catch_deleter.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/alfred/.hermes/hermes-agent</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CATCH_SECONDS</key><string>14400</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>${CATCH_LOG}</string>
  <key>StandardErrorPath</key><string>${CATCH_ERR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
PLIST

launchctl bootout "gui/${UID_NOW}/ai.hermes.catch-deleter.agents199" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NOW}" "$CATCH_PLIST"
launchctl kickstart -k "gui/${UID_NOW}/ai.hermes.catch-deleter.agents199"
sleep 1
echo "catcher_state:"
launchctl print "gui/${UID_NOW}/ai.hermes.catch-deleter.agents199" 2>/dev/null | awk '/state = |pid = |last exit code/ {print}' || true

echo "restarting gateways: ${LABELS[*]}"
for label in "${LABELS[@]}"; do
  echo "kickstart $label $(ts)"
  launchctl kickstart -k "gui/${UID_NOW}/${label}" || echo "kickstart_failed $label"
done
sleep 8

echo "post_pids:"
for label in "${LABELS[@]}"; do
  launchctl print "gui/${UID_NOW}/${label}" 2>/dev/null | awk -v label="$label" '/state = / {print label" state=" $3} /pid = / {print label" pid=" $3} /last exit code/ {print label" last_exit=" $5}' || true
done

echo "guard_import_probe:"
PYTHONPATH="$ROOT" /opt/homebrew/bin/python3.13 - <<'PY'
from tools import terminal_tool
print('has_guard=', hasattr(terminal_tool, '_tracked_file_delete_guard'))
print('guard_file=', terminal_tool.__file__)
PY

echo "integrity:"
/opt/homebrew/bin/python3.13 scripts/worktree_integrity_check.py || true

echo "catcher_log_tail:"
tail -n 8 /Users/alfred/.hermes/logs/catch-deleter.log 2>/dev/null || true

echo "===== AGENTS-199 LIVE DEPLOY END $(ts) ====="
