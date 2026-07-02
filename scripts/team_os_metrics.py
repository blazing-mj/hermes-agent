#!/usr/bin/env python3
"""team_os_metrics.py — Phase 4 growth-loop foundation: the system measures itself.

Reads the kanban boards and computes per-ticket + aggregate metrics:
cycle time, run stability (crashed/timed-out/spawn-failed), genuine vs inline
sessions, gate routing, interventions. Writes ~/.hermes/state/team-os-metrics.json
and prints a human digest. Read-only over the DBs; safe to run anytime/cron.

The weekly Reporter digest and "worst metric files its own improvement ticket"
build on this output.

Usage: python3 team_os_metrics.py [--days 7]
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERMES = Path.home() / ".hermes"
OUT = HERMES / "state" / "team-os-metrics.json"
TICKET_RE = re.compile(r"(AGENTS-\d+)")
BAD = ("crashed", "timed_out", "spawn_failed", "gave_up")


def collect(days: int) -> dict:
    cutoff = time.time() - days * 86400
    tickets: dict[str, dict] = defaultdict(lambda: {
        "tasks": 0, "done": 0, "blocked": 0, "runs": 0, "bad_runs": 0,
        "genuine_runs": 0, "inline_runs": 0, "interventions": 0,
        "first_start": None, "last_end": None, "boards": set(),
    })
    for db in glob.glob(str(HERMES / "kanban/boards/*/kanban.db")):
        board = Path(db).parent.name
        con = sqlite3.connect(db); con.row_factory = sqlite3.Row
        for t in con.execute("SELECT id,title,status FROM tasks").fetchall():
            m = TICKET_RE.search(t["title"] or "")
            if not m:
                continue
            tk = tickets[m.group(1)]
            runs = con.execute(
                "SELECT profile,outcome,started_at,ended_at,summary FROM task_runs WHERE task_id=?",
                (t["id"],)).fetchall()
            # recency filter: skip tickets with no activity in window
            stamps = [r["started_at"] for r in runs if r["started_at"]]
            if stamps and max(stamps) < cutoff:
                continue
            tk["boards"].add(board)
            tk["tasks"] += 1
            tk["done"] += t["status"] == "done"
            tk["blocked"] += t["status"] == "blocked"
            for r in runs:
                tk["runs"] += 1
                dur = (r["ended_at"] or 0) - (r["started_at"] or 0)
                if (r["outcome"] or "") in BAD:
                    tk["bad_runs"] += 1
                if r["profile"] or dur >= 60:
                    tk["genuine_runs"] += 1
                else:
                    tk["inline_runs"] += 1
                if re.search(r"controller stopped|manual|hand-", r["summary"] or "", re.I):
                    tk["interventions"] += 1
                if r["started_at"]:
                    tk["first_start"] = min(tk["first_start"] or r["started_at"], r["started_at"])
                if r["ended_at"]:
                    tk["last_end"] = max(tk["last_end"] or 0, r["ended_at"])
        con.close()

    per_ticket = {}
    for name, d in tickets.items():
        if not d["tasks"]:
            continue
        cyc = None
        if d["first_start"] and d["last_end"] and d["last_end"] > d["first_start"]:
            cyc = round((d["last_end"] - d["first_start"]) / 60)
        per_ticket[name] = {
            "boards": sorted(d["boards"]), "tasks": d["tasks"], "done": d["done"],
            "blocked": d["blocked"], "runs": d["runs"], "bad_runs": d["bad_runs"],
            "genuine_runs": d["genuine_runs"], "inline_runs": d["inline_runs"],
            "interventions": d["interventions"], "cycle_minutes": cyc,
        }

    runs = sum(t["runs"] for t in per_ticket.values())
    agg = {
        "window_days": days,
        "tickets_active": len(per_ticket),
        "total_runs": runs,
        "bad_run_rate": round(sum(t["bad_runs"] for t in per_ticket.values()) / runs, 3) if runs else None,
        "inline_run_rate": round(sum(t["inline_runs"] for t in per_ticket.values()) / runs, 3) if runs else None,
        "interventions": sum(t["interventions"] for t in per_ticket.values()),
        "median_cycle_minutes": None,
    }
    cycles = sorted(t["cycle_minutes"] for t in per_ticket.values() if t["cycle_minutes"])
    if cycles:
        agg["median_cycle_minutes"] = cycles[len(cycles) // 2]
    return {"generated": datetime.now().isoformat(timespec="seconds"),
            "aggregate": agg, "tickets": per_ticket}


def digest(data: dict) -> str:
    a = data["aggregate"]
    lines = [f"TEAM OS METRICS — last {a['window_days']}d  ({data['generated']})",
             f"  tickets active: {a['tickets_active']}   runs: {a['total_runs']}",
             f"  bad-run rate: {a['bad_run_rate']}   inline-run rate: {a['inline_run_rate']}"
             f"   interventions: {a['interventions']}",
             f"  median cycle: {a['median_cycle_minutes']} min", "", "  worst offenders:"]
    worst = sorted(data["tickets"].items(),
                   key=lambda kv: (kv[1]["bad_runs"], kv[1]["interventions"]), reverse=True)[:5]
    for name, t in worst:
        if t["bad_runs"] or t["interventions"] or t["inline_runs"]:
            lines.append(f"    {name}: bad={t['bad_runs']} inline={t['inline_runs']} "
                         f"interventions={t['interventions']} cycle={t['cycle_minutes']}min")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    data = collect(args.days)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    print(digest(data))
    print(f"\n  full data → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
