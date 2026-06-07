"""Tests for Team OS Phase 2 lane leases and watchers (AGENTS-191)."""
from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "team_os_lane_watcher.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("team_os_lane_watcher", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_simultaneous_ticks_cannot_both_claim_same_card(mod, tmp_path):
    barrier = threading.Barrier(2)
    results = []

    def tick(holder: str):
        barrier.wait(timeout=5)
        claim = mod.claim_card(
            issue="AGENTS-999",
            lane="Backlog",
            holder=holder,
            ledger_dir=tmp_path,
            ttl_seconds=300,
        )
        results.append(claim)

    threads = [threading.Thread(target=tick, args=(holder,)) for holder in ("cortex-a", "cortex-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    winners = [claim for claim in results if claim["claimed"]]
    losers = [claim for claim in results if not claim["claimed"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["reason"] == "already_leased"
    ledger = json.loads((tmp_path / "team-os-card-leases.json").read_text(encoding="utf-8"))
    assert list(ledger["leases"].keys()) == ["AGENTS-999"]
    assert ledger["leases"]["AGENTS-999"]["lease_id"] == winners[0]["lease_id"]


def test_cortex_backlog_watcher_claims_at_most_one_and_uses_gated_mover(mod, tmp_path):
    runner_calls = []

    def runner(argv, stdin=None):
        runner_calls.append((argv, stdin))
        if argv[:2] == [mod.LINEAR_AGENT, "list"]:
            return "AGENTS-1 First card\nAGENTS-2 Second card\n"
        return "moved"

    result = mod.run_lane_watcher(
        role="cortex",
        lane="Backlog",
        project="Hermes System",
        ledger_dir=tmp_path,
        runner=runner,
    )

    assert result["ok"] is True
    assert result["claimed"] == "AGENTS-1"
    assert result["moved_to"] == "Triage"
    status_calls = [call for call in runner_calls if call[0][:2] == [mod.LINEAR_AGENT, "status"]]
    assert len(status_calls) == 1
    assert status_calls[0][0] == [mod.LINEAR_AGENT, "status", "AGENTS-1", "Triage"]
    assert "AGENTS-2" not in json.dumps(json.loads((tmp_path / "team-os-card-leases.json").read_text()))


def test_cto_todo_watcher_claims_at_most_one_and_uses_gated_mover(mod, tmp_path):
    runner_calls = []

    def runner(argv, stdin=None):
        runner_calls.append((argv, stdin))
        if argv[:2] == [mod.LINEAR_AGENT, "list"]:
            return json.dumps([
                {"identifier": "AGENTS-21", "title": "Ready card"},
                {"identifier": "AGENTS-22", "title": "Later card"},
            ])
        return "moved"

    result = mod.run_lane_watcher(
        role="cto",
        lane="Todo",
        project="Hermes System",
        ledger_dir=tmp_path,
        runner=runner,
    )

    assert result["ok"] is True
    assert result["claimed"] == "AGENTS-21"
    assert result["moved_to"] == "In Progress"
    status_calls = [call for call in runner_calls if call[0][:2] == [mod.LINEAR_AGENT, "status"]]
    assert len(status_calls) == 1
    assert status_calls[0][0] == [mod.LINEAR_AGENT, "status", "AGENTS-21", "In Progress"]


def test_poll_lane_falls_back_to_unfiltered_list_when_state_filter_returns_empty(mod):
    calls = []

    def runner(argv, stdin=None):
        calls.append(argv)
        if "--state" in argv:
            return ""
        return "AGENTS-1 | Backlog | Hermes System | Match\nAGENTS-2 | Todo | Hermes System | Wrong lane\n"

    assert mod.poll_lane(lane="Backlog", project="Hermes System", runner=runner) == ["AGENTS-1"]
    assert len(calls) == 2


def test_cortex_triage_handoff_requires_grounding_and_contract_before_todo(mod, tmp_path):
    runner_calls = []

    def runner(argv, stdin=None):
        runner_calls.append((argv, stdin))
        return "moved"

    missing = mod.advance_claimed_card(
        issue="AGENTS-44",
        role="cortex",
        from_lane="Triage",
        grounding_doc=None,
        thin_contract=None,
        assignee="cto",
        ledger_dir=tmp_path,
        runner=runner,
    )
    assert missing["ok"] is False
    assert "grounding_doc" in missing["error"]
    assert runner_calls == []

    ok = mod.advance_claimed_card(
        issue="AGENTS-44",
        role="cortex",
        from_lane="Triage",
        grounding_doc={"schema": "team_os.grounding.v1", "sources": ["Linear AGENTS-44"]},
        thin_contract={"schema": "team_os.thin_contract.v1", "goal": "demo"},
        assignee="cto",
        ledger_dir=tmp_path,
        runner=runner,
    )
    assert ok["ok"] is True
    assert ok["moved_to"] == "Todo"
    assert runner_calls[-1][0] == [mod.LINEAR_AGENT, "status", "AGENTS-44", "Todo", "--assignee", "cto"]
    artifacts = json.loads((tmp_path / "team-os-card-artifacts.json").read_text(encoding="utf-8"))
    assert artifacts["AGENTS-44"]["grounding_doc"]["schema"] == "team_os.grounding.v1"
    assert artifacts["AGENTS-44"]["thin_contract"]["schema"] == "team_os.thin_contract.v1"
