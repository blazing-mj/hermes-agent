#!/usr/bin/env python3
"""Verify Team OS board flow slices.

Phase 2 scope only: single-card leases, Cortex Backlog watcher, CTO Todo watcher,
and Cortex Triage->Todo artifact gate.  The distinct real session-id auto-Done
proof remains intentionally outside this verifier for AGENTS-192.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WATCHER_PATH = ROOT / "scripts" / "team_os_lane_watcher.py"


def _load_watcher():
    spec = importlib.util.spec_from_file_location("team_os_lane_watcher", WATCHER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {WATCHER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pass(name: str, details: str = "") -> dict[str, Any]:
    return {"name": name, "status": "PASS", "details": details}


def _fail(name: str, details: str) -> dict[str, Any]:
    return {"name": name, "status": "FAIL", "details": details}


def verify_phase2() -> tuple[bool, list[dict[str, Any]]]:
    watcher = _load_watcher()
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="team-os-phase2-") as td:
        ledger_dir = Path(td)

        # Concurrency: two simultaneous attempts against same card; exactly one wins.
        barrier = threading.Barrier(2)
        claims: list[dict[str, Any]] = []

        def contender(holder: str) -> None:
            barrier.wait(timeout=5)
            claims.append(watcher.claim_card(issue="AGENTS-PHASE2", lane="Backlog", holder=holder, ledger_dir=ledger_dir))

        threads = [threading.Thread(target=contender, args=(h,)) for h in ("tick-a", "tick-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        winners = [c for c in claims if c.get("claimed")]
        losers = [c for c in claims if not c.get("claimed")]
        checks.append(
            _pass("single-card lease concurrency", "exactly one claim won")
            if len(winners) == 1 and len(losers) == 1 and losers[0].get("reason") == "already_leased"
            else _fail("single-card lease concurrency", json.dumps(claims, sort_keys=True))
        )

        # Cortex Backlog watcher: claims one and moves Backlog->Triage through writer route.
        cortex_calls = []

        def cortex_runner(argv, stdin=None):
            cortex_calls.append((argv, stdin))
            if argv[:2] == [watcher.LINEAR_AGENT, "list"]:
                return "AGENTS-101 First\nAGENTS-102 Second\n"
            return "moved"

        cortex = watcher.run_lane_watcher(role="cortex", lane="Backlog", ledger_dir=ledger_dir / "cortex", runner=cortex_runner)
        cortex_status = [c for c in cortex_calls if c[0][:2] == [watcher.LINEAR_AGENT, "status"]]
        checks.append(
            _pass("Cortex Backlog watcher", "claimed AGENTS-101 and moved to Triage")
            if cortex.get("ok") and cortex.get("claimed") == "AGENTS-101" and cortex.get("moved_to") == "Triage" and len(cortex_status) == 1
            else _fail("Cortex Backlog watcher", json.dumps(cortex, sort_keys=True))
        )

        # CTO Todo watcher: claims one and moves Todo->In Progress through writer route.
        cto_calls = []

        def cto_runner(argv, stdin=None):
            cto_calls.append((argv, stdin))
            if argv[:2] == [watcher.LINEAR_AGENT, "list"]:
                return json.dumps([{"identifier": "AGENTS-201"}, {"identifier": "AGENTS-202"}])
            return "moved"

        cto = watcher.run_lane_watcher(role="cto", lane="Todo", ledger_dir=ledger_dir / "cto", runner=cto_runner)
        cto_status = [c for c in cto_calls if c[0][:2] == [watcher.LINEAR_AGENT, "status"]]
        checks.append(
            _pass("CTO Todo watcher", "claimed AGENTS-201 and moved to In Progress")
            if cto.get("ok") and cto.get("claimed") == "AGENTS-201" and cto.get("moved_to") == "In Progress" and len(cto_status) == 1
            else _fail("CTO Todo watcher", json.dumps(cto, sort_keys=True))
        )

        # Triage handoff: must reject missing artifacts, then pass with schema'd grounding+contract.
        triage_calls = []

        def triage_runner(argv, stdin=None):
            triage_calls.append((argv, stdin))
            return "moved"

        missing = watcher.advance_claimed_card(issue="AGENTS-301", role="cortex", from_lane="Triage", ledger_dir=ledger_dir / "triage", runner=triage_runner)
        ok = watcher.advance_claimed_card(
            issue="AGENTS-301",
            role="cortex",
            from_lane="Triage",
            grounding_doc={"schema": "team_os.grounding.v1", "sources": ["verify_board_flow.py"]},
            thin_contract={"schema": "team_os.thin_contract.v1", "goal": "phase2 verifier"},
            assignee="cto",
            ledger_dir=ledger_dir / "triage",
            runner=triage_runner,
        )
        checks.append(
            _pass("Triage->Todo artifact gate", "missing artifacts rejected; valid artifacts moved")
            if not missing.get("ok") and ok.get("ok") and ok.get("moved_to") == "Todo" and len(triage_calls) == 1
            else _fail("Triage->Todo artifact gate", json.dumps({"missing": missing, "ok": ok}, sort_keys=True))
        )

        checks.append(_pass("AGENTS-192 session-id gate", "not evaluated in Phase 2; remains future Phase 3+ surface"))

    return all(c["status"] == "PASS" for c in checks), checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["2"], default="2")
    args = parser.parse_args()
    ok, checks = verify_phase2()
    for check in checks:
        print(f"{check['status']} {check['name']}: {check['details']}")
    print("PHASE 2 ALL-PASS" if ok else "PHASE 2 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
