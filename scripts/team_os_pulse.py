#!/usr/bin/env python3
"""team_os_pulse.py — one-command live status of the whole Team OS machine.

Answers "what is happening right now?": board lanes, active chains, motor health
(sweep tick + webhook deliveries), gateways, integrity. Read-only, fail-soft.

Usage: python3 team_os_pulse.py
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERMES = Path.home() / ".hermes"
LINEAR = HERMES / "bin" / "linear-agent"
PROJECTS = ["Hermes System", "OpenClaw Core"]
B = "\033[1m"; G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; N = "\033[0m"


def sh(args: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def board() -> None:
    print(f"\n{B}── BOARD ──{N}")
    for p in PROJECTS:
        rows = [l for l in sh([str(LINEAR), "list", "--project", p]).splitlines() if " | " in l]
        lanes: dict[str, int] = {}
        attention = []
        for l in rows:
            parts = [x.strip() for x in l.split("|")]
            if len(parts) < 2:
                continue
            lanes[parts[1]] = lanes.get(parts[1], 0) + 1
            if parts[1] in ("Needs-MJ", "Approved", "Blocked"):
                attention.append(f"{parts[0]} [{parts[1]}] {parts[3][:48] if len(parts)>3 else ''}")
        summary = "  ".join(f"{k}:{v}" for k, v in sorted(lanes.items()) if k not in ("Done", "Canceled", "Duplicate"))
        print(f"  {p}: {summary or 'empty'}")
        for a in attention:
            print(f"    {Y}→ {a}{N}")


def chains() -> None:
    print(f"\n{B}── ACTIVE CHAINS (kanban) ──{N}")
    found = False
    for db in glob.glob(str(HERMES / "kanban/boards/*/kanban.db")):
        con = sqlite3.connect(db); con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT status, title FROM tasks WHERE status NOT IN ('done','archived','cancelled','canceled') "
            "ORDER BY rowid DESC LIMIT 8").fetchall()
        for r in rows:
            found = True
            color = Y if r["status"] in ("blocked", "review") else G
            print(f"  {color}{r['status']:9}{N} {os.path.basename(os.path.dirname(db)):14} {r['title'][:58]}")
        con.close()
    if not found:
        print("  (none in flight)")


def motors() -> None:
    print(f"\n{B}── MOTORS ──{N}")
    # sweep: newest cron/intake evidence
    led = HERMES / "state" / "team-os-cortex.db"
    if led.exists():
        age = int(time.time() - led.stat().st_mtime)
        col = G if age < 2400 else R
        print(f"  sweep/intake ledger: {col}last touched {age//60} min ago{N}")
    else:
        print(f"  sweep/intake ledger: {R}missing{N}")
    # webhook deliveries today
    glog = HERMES / "logs" / "gateway.log"
    today = datetime.now().strftime("%Y-%m-%d")
    hits = 0
    try:
        for line in open(glog, errors="ignore"):
            if today in line and re.search(r"webhook.*(received|handled|POST /)", line, re.I):
                hits += 1
    except OSError:
        pass
    col = G if hits else R
    print(f"  linear webhook deliveries today: {col}{hits}{N}" + ("  ← doorbell dead" if not hits else ""))


def fleet() -> None:
    print(f"\n{B}── FLEET ──{N}")
    out = sh(["launchctl", "list"])
    up = [l.split("\t")[2] for l in out.splitlines() if "ai.hermes.gateway" in l and not l.startswith("-") and "watchdog" not in l]
    print(f"  gateways up: {len(up)}/5  ({', '.join(s.replace('ai.hermes.gateway','def').replace('-','') or 'default' for s in sorted(up))})")
    # sys.executable: run the sibling check with THIS interpreter — a hardcoded
    # /opt/homebrew/bin/python3.13 died when brew rotated to 3.14 (2026-07-02).
    integ = sh([sys.executable, str(HERMES / "scripts" / "worktree_integrity_check.py")])
    ok = "OK" in integ
    print(f"  integrity: {G+'OK'+N if ok else R+'PROBLEMS — run worktree_integrity_check.py'+N}")
    # repo branch sanity
    branch = sh(["git", "-C", str(HERMES / "hermes-agent"), "branch", "--show-current"]).strip()
    col = G if branch == "main" else Y
    print(f"  live checkout branch: {col}{branch}{N}" + ("  ← not main (unmerged live code)" if branch != "main" else ""))


def streak() -> None:
    print(f"\n{B}── AUTONOMY STREAK ──{N}")
    f = HERMES / "state" / "team-os-streak.json"
    try:
        import json as _json
        d = _json.loads(f.read_text())
        n, tgt = d.get("count", 0), d.get("target", 5)
        bar = G + "●" * n + N + "○" * max(0, tgt - n)
        print(f"  {bar}  {n}/{tgt} clean runs (reviewer-graded)")
        for r in d.get("runs", [])[-3:]:
            print(f"    ✓ {r['ticket']} — {r['verdict']}")
    except Exception:
        print("  (no streak state)")


def main() -> int:
    print(f"{B}TEAM OS PULSE{N}  {datetime.now():%Y-%m-%d %H:%M}")
    board(); chains(); motors(); streak(); fleet()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
