#!/usr/bin/env python3
"""gateway_health_watch.py — Phase 0.5 observability floor (TeamOS eval-flywheel brief §4).

Alerts MJ on Telegram when the Hermes gateway goes SILENT or the model chain
TERMINALLY fails — so a multi-hour outage is caught by an alert, not by accident.

Two detectors, both tuned against REAL verified log signatures to avoid
false-positives on normal idle / recovered retries:

  1. SILENCE — the gateway logs a `gateway.memory_monitor` heartbeat every ~5
     min. If the newest gateway.log line is older than HEARTBEAT_MAX_AGE, the
     gateway is hung/dead → alert.
  2. TERMINAL MODEL-CHAIN FAILURE — a burst of *terminal* auth/credential
     errors (codex OAuth missing, token expired, AuthenticationError/401, no
     fallback) in the new log window, with NO recovery marker
     (`falling back to` / `switched to` / `connected`) → alert. Transient,
     auto-recovered failures (e.g. `marking openrouter unhealthy`) are ignored
     by design — they are normal fallback behavior, not an outage.

Fail-closed + no-spam:
  - A cursor (byte offset per log file) means only NEW lines are scanned.
  - A cooldown per alert-kind (default 30 min) prevents re-alerting a standing
    outage every tick.
  - State persisted at ~/.hermes/state/gateway-health-watch.json.

This script performs NO mutations to any system — it reads logs and sends a
Telegram message. Safe to run while Team OS is paused (it is monitoring, not
Team OS activation).

Run: python3 gateway_health_watch.py            # one tick (cron/launchd)
     python3 gateway_health_watch.py --dry-run  # detect + print, no send
     python3 gateway_health_watch.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

H = Path.home()
LOG_DIR = H / ".hermes" / "logs"
GATEWAY_LOG = LOG_DIR / "gateway.log"
ERROR_LOG = LOG_DIR / "gateway.error.log"
STATE_PATH = H / ".hermes" / "state" / "gateway-health-watch.json"
MJ_TELEGRAM_CHAT = os.environ.get("MJ_TELEGRAM_CHAT", "170258889")

# Newest gateway.log line older than this ⇒ silent. The memory_monitor beats
# every 5 min, so 15 min = ~3 missed beats — unambiguous, no idle false-positive.
HEARTBEAT_MAX_AGE_SEC = int(os.environ.get("GW_HEARTBEAT_MAX_AGE", "900"))
# Min count of terminal-failure hits in the new window to call it an outage.
TERMINAL_BURST_MIN = int(os.environ.get("GW_TERMINAL_BURST_MIN", "3"))
# Per-alert-kind cooldown so a standing outage alerts once, not every tick.
ALERT_COOLDOWN_SEC = int(os.environ.get("GW_ALERT_COOLDOWN", "1800"))

# Verified terminal signatures (see brief §4 + live-log verification 2026-06-27).
# These mean the chain could not serve the call — auth/credential/no-fallback.
_TERMINAL_RE = re.compile(
    r"no Codex OAuth token|token is expired|token_expired|AuthenticationError|"
    r"\bno fallback\b|all providers (?:failed|exhausted|unavailable)|"
    r"could not generate a response",
    re.I,
)
# MODEL-CHAIN recovery markers only: the chain found an alternative provider in
# the same window ⇒ the terminal failure was recovered, not an outage. Kept
# deliberately NARROW — transport/channel signals (✓ connected, response sent)
# are NOT model recovery, and including them risks suppressing a real
# alive-but-model-failing outage (a false-negative, the worst failure for a
# safety floor).
_RECOVERY_RE = re.compile(r"falling back to|switched to|retrying with", re.I)
# Log line timestamp: "2026-06-27 20:17:05,760 ..."
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass
class Alert:
    kind: str           # "silent" | "model_chain_failed"
    message: str
    detail: str = ""


@dataclass
class WatchInputs:
    """Everything the pure detector needs — injectable for tests."""
    now: float
    newest_log_ts: float | None          # epoch of newest gateway.log line, or None
    new_error_lines: list[str] = field(default_factory=list)


def parse_ts(line: str, *, year_tz_naive: bool = True) -> float | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def detect(inp: WatchInputs) -> list[Alert]:
    """PURE detector — no I/O. Returns the alerts that should fire this tick."""
    alerts: list[Alert] = []

    # 1. Silence / hung gateway.
    if inp.newest_log_ts is not None:
        age = inp.now - inp.newest_log_ts
        if age > HEARTBEAT_MAX_AGE_SEC:
            mins = int(age // 60)
            alerts.append(Alert(
                "silent",
                f"⚠️ Hermes gateway SILENT — no log activity for {mins} min "
                f"(heartbeat beats every ~5 min). It may be hung or down.",
                detail=f"age={mins}m threshold={HEARTBEAT_MAX_AGE_SEC // 60}m",
            ))

    # 2. Terminal model-chain failure in the new window, with no recovery.
    terminal_hits = [l for l in inp.new_error_lines if _TERMINAL_RE.search(l)]
    recovered = any(_RECOVERY_RE.search(l) for l in inp.new_error_lines)
    if len(terminal_hits) >= TERMINAL_BURST_MIN and not recovered:
        sample = terminal_hits[-1].strip()[:160]
        alerts.append(Alert(
            "model_chain_failed",
            f"⚠️ Hermes model chain FAILING — {len(terminal_hits)} terminal "
            f"auth/credential errors with no recovery. Agents likely can't respond.",
            detail=f"last: {sample}",
        ))
    return alerts


# ── I/O layer (cursor, state, send) ─────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def _read_new_lines(path: Path, cursor_key: str, state: dict) -> list[str]:
    """Return lines appended since the last run; advance the cursor. Handles
    truncation/rotation (file shrank ⇒ start from 0)."""
    if not path.exists():
        return []
    size = path.stat().st_size
    start = int(state.get(cursor_key, 0))
    if start > size:           # rotated/truncated
        start = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(start)
        data = f.read()
        state[cursor_key] = f.tell()
    return data.splitlines()


def _newest_log_ts(path: Path) -> float | None:
    """Epoch of the newest timestamped line in the tail of the log."""
    if not path.exists():
        return None
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(max(0, size - 8192))
        tail = f.read().splitlines()
    for line in reversed(tail):
        ts = parse_ts(line)
        if ts is not None:
            return ts
    return None


def _load_env() -> None:
    """Load the Telegram bot token from ~/.hermes/.env so a launchd/cron run
    can send without relying on ambient env. Best-effort, never raises."""
    env = H / ".hermes" / ".env"
    if not env.exists():
        return
    try:
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if "TOKEN" in k or "TELEGRAM" in k:
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except OSError:
        pass


def _send_telegram(message: str) -> bool:
    try:
        import sys
        _load_env()
        sys.path.insert(0, str(H / ".hermes" / "hermes-agent"))
        from tools.send_message_tool import send_message_tool
        raw = send_message_tool({"target": f"telegram:{MJ_TELEGRAM_CHAT}", "message": message})
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return bool(isinstance(parsed, dict) and parsed.get("success"))
    except Exception:  # noqa: BLE001 - alerting must never crash the watcher
        return False


def run_tick(*, dry_run: bool = False) -> dict:
    state = _load_state()
    now = time.time()
    new_err = _read_new_lines(ERROR_LOG, "error_cursor", state)
    new_gw = _read_new_lines(GATEWAY_LOG, "gateway_cursor", state)
    inp = WatchInputs(now=now, newest_log_ts=_newest_log_ts(GATEWAY_LOG),
                      new_error_lines=new_err + new_gw)
    alerts = detect(inp)

    fired, suppressed = [], []
    last_alert = state.setdefault("last_alert_at", {})
    for a in alerts:
        if now - float(last_alert.get(a.kind, 0)) < ALERT_COOLDOWN_SEC:
            suppressed.append(a.kind)
            continue
        sent = True if dry_run else _send_telegram(a.message + (f"\n{a.detail}" if a.detail else ""))
        if sent:
            last_alert[a.kind] = now
            fired.append(a.kind)
    if not dry_run:
        _save_state(state)
    return {"fired": fired, "suppressed": suppressed,
            "alerts": [(a.kind, a.message) for a in alerts],
            "new_lines": len(new_err) + len(new_gw)}


def _self_test() -> int:
    """Inline detector checks (no I/O, no tokens)."""
    now = 1_700_000_000.0
    # silent
    a = detect(WatchInputs(now=now, newest_log_ts=now - 1200, new_error_lines=[]))
    assert any(x.kind == "silent" for x in a), "should alert on stale heartbeat"
    # alive, no failures → nothing
    a = detect(WatchInputs(now=now, newest_log_ts=now - 60, new_error_lines=["all good"]))
    assert a == [], "idle/alive must not alert"
    # terminal burst, no recovery → alert
    burst = ["no Codex OAuth token found"] * 3
    a = detect(WatchInputs(now=now, newest_log_ts=now - 60, new_error_lines=burst))
    assert any(x.kind == "model_chain_failed" for x in a), "terminal burst should alert"
    # terminal burst BUT recovered → suppressed
    a = detect(WatchInputs(now=now, newest_log_ts=now - 60,
                           new_error_lines=burst + ["falling back to anthropic"]))
    assert not any(x.kind == "model_chain_failed" for x in a), "recovered burst must NOT alert"
    # single transient failure → no alert (below burst threshold)
    a = detect(WatchInputs(now=now, newest_log_ts=now - 60,
                           new_error_lines=["marking openrouter unhealthy"]))
    assert a == [], "transient fallback must not alert"
    print("self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes gateway health watcher (Phase 0.5)")
    ap.add_argument("--dry-run", action="store_true", help="detect + print, do not send")
    ap.add_argument("--self-test", action="store_true", help="run inline detector tests")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    result = run_tick(dry_run=args.dry_run)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
