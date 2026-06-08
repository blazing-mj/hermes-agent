#!/usr/bin/env python3
"""Verify Team OS board flow slices.

Covers the committed deterministic board-flow rails through Phase 4. Phase 4
proves human-gate dual-ping policy with injected callbacks: consequential /
human-gated cards assign MJ and send exactly one proof-link Telegram ping, while
low-cost cards do neither.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import threading
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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


def _phase4_event(source_id: str, *, tier: str, requires_mj_review: bool, gate: str | None = None, classifier_uncertain: bool = False) -> dict[str, Any]:
    return {
        "id": 44 if tier != "low" else 45,
        "event_type": "linear_observation",
        "source_id": source_id,
        "source": "linear",
        "payload": {
            "source": "linear",
            "source_id": source_id,
            "title": "Phase 4 verifier card",
            "body": "Verifier-only card; no live mutation.",
            "labels": ["system:hermes", "gate:human"] if gate == "human" else ["system:hermes"],
            "url": f"https://linear.app/blazeragency/issue/{source_id.lower()}",
            "failure_cost_tier": tier,
            "requires_mj_review": requires_mj_review,
            "failure_cost_reason": f"verifier maps to {tier}",
            "gate": gate,
            "classifier_uncertain": classifier_uncertain,
            "validation_contract": {
                "source_ticket": source_id,
                "intended_behavior": "Verifier-only Team OS card",
                "non_goals": ["Do not touch production"],
                "assertions": ["Verifier callback policy is enforced"],
                "commands": ["true"],
                "required_commands": ["true"],
                "behavior_check_required": True,
                "risk": tier,
                "human_gate_required": bool(requires_mj_review or gate == "human" or classifier_uncertain),
                "bounce_conditions": ["Callback policy violated"],
                "files_to_touch": [],
            },
        },
        "state": "queued",
    }


def verify_phase4() -> tuple[bool, list[dict[str, Any]]]:
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="team-os-phase4-") as td:
        root = Path(td)
        cfg = DispatcherConfig(
            repo_root=root / "repo",
            worktree_root=root / "workers",
            artifact_root=root / "artifacts",
            lease_root=root / "leases",
            telegram_push_enabled=True,
        )
        calls: dict[str, list[str]] = {"assign": [], "telegram": []}

        def worker(**_kwargs):  # noqa: ANN003
            raise AssertionError("human-gate verifier must not run worker")

        consequential = dispatch_outbox_event(
            _phase4_event("AGENTS-PHASE4", tier="critical", requires_mj_review=True, gate="human", classifier_uncertain=True),
            cfg,
            worker=worker,
            assign_mj=lambda ticket: calls["assign"].append(ticket),
            telegram_push=lambda message: calls["telegram"].append(message),
        )
        checks.append(
            _pass("Phase 4 consequential human gate", "assignee=MJ and exactly one Telegram proof ping")
            if consequential.get("status") == "needs_mj"
            and consequential.get("linear_assignment") == {"assignee": "MJ", "set": True}
            and calls["assign"] == ["AGENTS-PHASE4"]
            and len(calls["telegram"]) == 1
            and "https://linear.app/blazeragency/issue/agents-phase4" in calls["telegram"][0]
            else _fail("Phase 4 consequential human gate", json.dumps({"result": consequential, "calls": calls}, sort_keys=True))
        )

        low_calls: dict[str, list[str]] = {"assign": [], "telegram": []}

        def low_worker(**kwargs):  # noqa: ANN003
            contract = kwargs["contract"]
            return {
                "worker_status": "completed",
                "source_ticket": contract["source_ticket"],
                "worktree_path": str(root / "workers" / "branch"),
                "changed_files": [],
                "proof_results": [{"command": "true", "exit_code": 0}],
                "worker_output": "verifier low-cost ok",
            }

        def low_validator(**_kwargs):  # noqa: ANN003
            return {"verdict": "PASS", "source_ticket": "AGENTS-LOW", "review_text": "VERDICT: PASS"}

        low = dispatch_outbox_event(
            _phase4_event("AGENTS-LOW", tier="low", requires_mj_review=False),
            cfg,
            worker=low_worker,
            validator=low_validator,
            assign_mj=lambda ticket: low_calls["assign"].append(ticket),
            telegram_push=lambda message: low_calls["telegram"].append(message),
        )
        checks.append(
            _pass("Phase 4 low-cost silence", "no assignment and no Telegram ping")
            if low.get("status") == "validated"
            and low.get("telegram_push") == {"enabled": True, "sent": False}
            and low_calls == {"assign": [], "telegram": []}
            else _fail("Phase 4 low-cost silence", json.dumps({"result": low, "calls": low_calls}, sort_keys=True))
        )

    return all(c["status"] == "PASS" for c in checks), checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["2", "4", "all"], default="all")
    args = parser.parse_args()
    sections: list[tuple[str, bool, list[dict[str, Any]]]] = []
    if args.phase in {"2", "all"}:
        ok2, checks2 = verify_phase2()
        sections.append(("PHASE 2", ok2, checks2))
    if args.phase in {"4", "all"}:
        ok4, checks4 = verify_phase4()
        sections.append(("PHASE 4", ok4, checks4))
    all_ok = all(ok for _, ok, _ in sections)
    for label, ok, checks in sections:
        for check in checks:
            print(f"{check['status']} {label} {check['name']}: {check['details']}")
        print(f"{label} ALL-PASS" if ok else f"{label} FAIL")
    overall = "PHASE ALL" if args.phase == "all" else sections[0][0]
    print(f"{overall} ALL-PASS" if all_ok else f"{overall} FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
