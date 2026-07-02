#!/usr/bin/env python3
"""team_os_control.py — the PAUSE / RESUME button for the autonomous loop.

Writes the canonical kill-switch state file that the intake motor checks before
picking any card. Paused = the sweep/doorbell stop picking new work (in-flight
chains finish; nothing new starts). MJ's emergency brake.

Usage:
  team_os_control.py status
  team_os_control.py pause   ["reason"]
  team_os_control.py resume
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

STATE = Path("~/.hermes/state/team-os-kill-switch.json").expanduser()


def _read() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"enabled": False}


def _write(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), prefix=".ks.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, STATE)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    s = _read()
    if cmd == "status":
        on = s.get("enabled")
        print(f"\n  Team OS autonomous loop: {'⏸  PAUSED' if on else '▶  RUNNING'}")
        if on:
            print(f"  reason: {s.get('reason','(none)')}")
            print(f"  since:  {s.get('ts','?')}")
        print("  (pause stops NEW picks; in-flight chains still finish)\n")
    elif cmd == "pause":
        reason = sys.argv[2] if len(sys.argv) > 2 else "MJ manual pause"
        _write({"enabled": True, "reason": reason, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        print(f"\n  ⏸  PAUSED — intake will stop picking new cards.\n  reason: {reason}\n")
    elif cmd == "resume":
        _write({"enabled": False, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        print("\n  ▶  RESUMED — autonomous loop active again.\n")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
