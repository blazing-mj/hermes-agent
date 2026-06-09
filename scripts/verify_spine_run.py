#!/usr/bin/env python3.13
"""verify_spine_run.py — deterministic scorecard for one Team OS spine run.

Codifies the reviewer's manual checks: given a Linear ticket id (e.g. AGENTS-206),
finds its kanban task chain across boards and grades the run. FAIL-CLOSED:
anything unprovable scores FAIL/WARN, never silent pass.

Checks: all stages present (cortex→cto→worker→validator) · role separation
(validator must be the claude-max rail, never a gateway profile — the "Ruta rule")
· instability (crashed/timed-out/spawn-failed runs) · manual interventions
(controller hand-stops) · validator verdict · duration.

Usage: python3.13 verify_spine_run.py AGENTS-206 [AGENTS-188 ...]
Exit: 0 all PASS/WARN, 1 any FAIL.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

BOARDS = glob.glob(os.path.expanduser("~/.hermes/kanban/boards/*/kanban.db")) + \
         [os.path.expanduser("~/.hermes/kanban.db")]
GATEWAY_PROFILES = {"default", "cortex", "cto", "ruta", "billprinter"}
# Order matters: most-specific first — validator/cto names beat generic keywords.
STAGE_PATTERNS = [
    ("validator", r"validator|independent proof"),
    ("cto",       r"\bcto\b|contract"),
    ("cortex",    r"cortex|triage|grounding"),
    ("worker",    r"worker|wire|implement|upgrade|helper"),
]
C = {"PASS": "\033[92mPASS\033[0m", "WARN": "\033[93mWARN\033[0m", "FAIL": "\033[91mFAIL\033[0m"}


def classify(title: str) -> str | None:
    t = title.lower()
    for stage, pat in STAGE_PATTERNS:
        if re.search(pat, t):
            return stage
    return None


def collect(ticket: str) -> tuple[str | None, dict]:
    """Return (board, {stage: {task, runs, comments}}) for the ticket's chain."""
    for db in BOARDS:
        if not os.path.exists(db):
            continue
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        tasks = con.execute(
            "SELECT * FROM tasks WHERE title LIKE ? ORDER BY rowid", (f"%{ticket}%",)
        ).fetchall()
        if not tasks:
            con.close()
            continue
        chain: dict = {}
        for t in tasks:
            stage = classify(t["title"]) or f"other:{t['id']}"
            runs = [dict(r) for r in con.execute(
                "SELECT profile,status,outcome,started_at,ended_at,summary,error "
                "FROM task_runs WHERE task_id=? ORDER BY rowid", (t["id"],)).fetchall()]
            comments = [str(r[0]) for r in con.execute(
                "SELECT body FROM task_comments WHERE task_id=? ORDER BY rowid", (t["id"],)).fetchall()]
            # keep the LAST task per stage (reruns supersede)
            chain[stage] = {"task": dict(t), "runs": runs, "comments": comments}
        board = os.path.basename(os.path.dirname(db)) or "root"
        con.close()
        return board, chain
    return None, {}


def grade(ticket: str) -> int:
    board, chain = collect(ticket)
    rows: list[tuple[str, str, str]] = []
    if not chain:
        print(f"\n  {ticket}: {C['FAIL']} no kanban task chain found on any board")
        return 1

    # 1. stages present
    for stage, _ in STAGE_PATTERNS:
        if stage in chain:
            st = chain[stage]["task"]["status"]
            ok = st in ("done",) or (stage == "worker" and st == "blocked")  # blocked=review gate pre-Integrator
            rows.append(("PASS" if ok else "FAIL", f"stage {stage} present", f"status={st}"))
        else:
            rows.append(("FAIL", f"stage {stage} present", "MISSING"))

    all_runs = [r for s in chain.values() for r in s["runs"]]
    all_comments = [c for s in chain.values() for c in s["comments"]]

    # 2. Ruta rule: validator must never run as a gateway profile
    v = chain.get("validator")
    if v:
        bad = [r["profile"] for r in v["runs"] if (r.get("profile") or "") in GATEWAY_PROFILES
               and r.get("outcome") not in ("blocked",)]
        joined = " ".join(v["comments"]).lower()
        claude_max = "claude" in joined and "pass" in joined
        if bad:
            rows.append(("FAIL", "validator independence (claude-max rail only)", f"ran as profile(s): {set(bad)}"))
        elif claude_max:
            rows.append(("PASS", "validator independence (claude-max rail only)", "claude-max PASS in comments"))
        else:
            rows.append(("WARN", "validator independence (claude-max rail only)", "no claude-max PASS evidence found"))

    # 3. instability: crashed/timed_out/spawn_failed runs
    unstable = [r for r in all_runs if (r.get("outcome") or "") in ("crashed", "timed_out", "spawn_failed", "gave_up")]
    rows.append(("PASS" if not unstable else "WARN" if len(unstable) <= 2 else "FAIL",
                 "run stability", f"{len(unstable)} crashed/timed-out/spawn-failed run(s)"))

    # 4. manual interventions (controller hand-stops, manual fixes)
    manual = [r for r in all_runs if re.search(r"controller stopped|manual|hand-", (r.get("summary") or ""), re.I)]
    manual += [1 for c in all_comments if re.search(r"manually created|hand-fixed|manual mkdir|re-?dispatched manually", c, re.I)]
    rows.append(("PASS" if not manual else "FAIL", "zero manual interventions",
                 f"{len(manual)} intervention sign(s)" if manual else "clean"))

    # 5. validator verdict recorded
    verdict = any(re.search(r"verdict:?\s*PASS", c, re.I) for c in all_comments)
    rows.append(("PASS" if verdict else "WARN", "validator verdict recorded", "PASS found" if verdict else "not found in comments"))

    # 6. duration
    starts = [r["started_at"] for r in all_runs if r.get("started_at")]
    ends = [r["ended_at"] for r in all_runs if r.get("ended_at")]
    dur = (max(ends) - min(starts)) / 60 if starts and ends else None
    rows.append(("PASS" if dur is not None else "WARN", "duration measured",
                 f"{dur:.0f} min across chain" if dur is not None else "no run timestamps"))

    # print
    print(f"\n  ── spine run scorecard: {ticket}  (board: {board}) ──")
    fails = 0
    for status, name, detail in rows:
        fails += status == "FAIL"
        print(f"    {C[status]}  {name:48} · {detail}")
    extra = [k for k in chain if k.startswith("other:")]
    if extra:
        print(f"    note: {len(extra)} unclassified task(s) in chain (review titles)")
    print(f"    => {'CLEAN RUN' if fails == 0 else f'{fails} FAIL(s) — not a clean run'}")
    return 1 if fails else 0


def main() -> int:
    tickets = [a for a in sys.argv[1:] if a.upper().startswith("AGENTS-")]
    if not tickets:
        print("usage: verify_spine_run.py AGENTS-NNN [...]")
        return 2
    print(f"  [{datetime.now():%Y-%m-%d %H:%M}] verify_spine_run — autonomy-gate grader (fail-closed)")
    return max(grade(t.upper()) for t in tickets)


if __name__ == "__main__":
    raise SystemExit(main())
