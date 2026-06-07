"""Tests for restricted_linear_writer.py (AGENTS-150 + AGENTS-190 Phase 1)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path("/Users/alfred/.hermes/scripts/restricted_linear_writer.py")
TRANSITIONS_PATH = Path("/Users/alfred/.hermes/hermes-agent/docs/team-os/board-transitions.json")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("restricted_linear_writer", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_denied_actions_never_call_runner(mod):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "SHOULD_NOT_RUN"

    proposal = {"actions": [{"action": "status", "issue": "AGENTS-1", "state": "Done"}, {"action": "close", "issue": "AGENTS-2"}]}
    result = mod.execute_proposal(proposal, runner=runner)

    assert result["ok"] is False
    assert result["executed"] == 0
    assert result["denied"] == 2
    assert calls == []


@pytest.mark.parametrize("field", ["issueUpdate", "stateId", "assigneeId"])
def test_blocked_payload_field_denied(mod, field):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "SHOULD_NOT_RUN"

    proposal = {"action": "create", "title": "My title", "description": f"use {field} here"}
    result = mod.execute_proposal(proposal, runner=runner)

    assert result["ok"] is False
    assert result["executed"] == 0
    assert result["denied"] == 1
    assert field in result["messages"][0]
    assert calls == []


def test_nested_graphql_like_payload_denied_even_when_surface_fields_safe(mod):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "SHOULD_NOT_RUN"

    proposal = {
        "action": "comment",
        "issue": "AGENTS-1",
        "body": "safe",
        "graphql": {"mutation": "issueUpdate", "stateId": "done", "assigneeId": "user-1"},
    }
    result = mod.execute_proposal(proposal, runner=runner)

    assert result["ok"] is False
    assert result["executed"] == 0
    assert calls == []


def test_allowed_actions_build_expected_linear_agent_argv(mod):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "ok"

    proposal = {
        "actions": [
            {"action": "list", "project": "Hermes System", "first": 5, "state": "Todo"},
            {"action": "issue", "issue": "AGENTS-99"},
            {
                "action": "create",
                "title": "Implement feature X",
                "description": "evidence",
                "project": "Hermes System",
                "labels": ["system:hermes", "type:ops"],
                "priority": 3,
            },
            {"action": "comment", "issue": "AGENTS-42", "body": "Ship it"},
        ]
    }
    result = mod.execute_proposal(proposal, runner=runner)

    assert result["ok"] is True
    assert result["executed"] == 4
    assert calls[0][0] == [mod.LINEAR_AGENT, "list", "--first", "5", "--project", "Hermes System", "--state", "Todo"]
    assert calls[1][0] == [mod.LINEAR_AGENT, "issue", "AGENTS-99"]
    assert calls[2][0] == [
        mod.LINEAR_AGENT,
        "create",
        "Implement feature X",
        "--description",
        "evidence",
        "--project",
        "Hermes System",
        "--priority",
        "3",
        "--label",
        "system:hermes",
        "--label",
        "type:ops",
    ]
    assert calls[3] == ([mod.LINEAR_AGENT, "comment", "AGENTS-42", "-"], "Ship it")


def test_default_runner_uses_subprocess_without_shell(mod, monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = mod.execute_proposal({"action": "list"})

    assert result["ok"] is True
    assert calls[0][0] == [mod.LINEAR_AGENT, "list"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 60


def test_secrets_sanitized_before_runner_and_output(mod):
    seen = []

    def runner(argv, stdin=None):
        seen.append((argv, stdin))
        return "stdout lin_api_abcdefghij1234567890xyz"

    raw_key = "lin_api_secretsecretsecretsecret"
    result = mod.execute_proposal({"action": "comment", "issue": "AGENTS-1", "body": f"key is {raw_key}"}, runner=runner)

    assert result["ok"] is True
    assert "lin_api_secret" not in seen[0][1]
    assert "lin_api_abcdefgh" not in result["messages"][0]
    assert "[REDACTED]" in seen[0][1]
    assert "[REDACTED]" in result["messages"][0]


def test_cli_dry_run_does_not_call_real_linear_agent(tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"action": "comment", "issue": "AGENTS-1", "body": "safe"}), encoding="utf-8")

    proc = subprocess.run(
        ["python3.13", str(SCRIPT_PATH), "--dry-run", "--proposal-file", str(proposal)],
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["executed"] == 1
    assert "linear-agent" in payload["messages"][0]
    assert "comment" in payload["messages"][0]


def test_every_transition_allowlist_move_is_accepted_by_status_gate(mod, tmp_path):
    spec = json.loads(TRANSITIONS_PATH.read_text(encoding="utf-8"))
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return json.dumps({"issue": "AGENTS-1", "state": {"name": argv[3]}})

    actions = [
        {"action": "status", "issue": "AGENTS-1", "from": t["from"], "to": t["to"], "by": actor, "conditions_met": t.get("conditions", [])}
        for t in spec["transitions"]
        for actor in t["by"]
    ]
    result = mod.execute_proposal({"actions": actions}, runner=runner, ledger_dir=tmp_path)

    assert result["ok"] is True
    assert result["executed"] == len(actions)
    assert result["denied"] == 0
    assert len(calls) == len(actions)
    assert all(call[0][1] == "status" for call in calls)


def test_illegal_status_moves_are_rejected_before_runner(mod, tmp_path):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "SHOULD_NOT_RUN"

    illegal_actions = [
        {"action": "status", "issue": "AGENTS-1", "from": "Backlog", "to": "Done", "by": "cortex"},
        {"action": "status", "issue": "AGENTS-1", "from": "Triage", "to": "Done", "by": "cortex"},
        {"action": "status", "issue": "AGENTS-1", "from": "Needs-MJ", "to": "Done", "by": "cortex"},
        {"action": "status", "issue": "AGENTS-1", "from": "Todo", "to": "In Progress", "by": "cortex"},
        {"action": "status", "issue": "AGENTS-1", "from": "In Progress", "to": "In Review", "by": "cortex"},
        {"action": "status", "issue": "AGENTS-1", "from": "In Review", "to": "Done", "by": "validator", "conditions_met": ["classified_low_cost"]},
    ]
    result = mod.execute_proposal({"actions": illegal_actions}, runner=runner, ledger_dir=tmp_path)

    assert result["ok"] is False
    assert result["executed"] == 0
    assert result["denied"] == len(illegal_actions)
    assert calls == []


def test_standalone_assignee_update_is_rejected(mod, tmp_path):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "SHOULD_NOT_RUN"

    result = mod.execute_proposal(
        {"action": "assign", "issue": "AGENTS-1", "assignee": "cto"},
        runner=runner,
        ledger_dir=tmp_path,
    )

    assert result["ok"] is False
    assert result["executed"] == 0
    assert result["denied"] == 1
    assert calls == []


def test_assignee_update_is_only_available_inside_allowed_status_move(mod, tmp_path):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "ok"

    legal = mod.execute_proposal(
        {"action": "status", "issue": "AGENTS-1", "from": "Triage", "to": "Todo", "by": "cortex", "conditions_met": ["grounding_and_contract_present", "assignee_set_cto", "human_gate_label_absent"], "assignee": "cto"},
        runner=runner,
        ledger_dir=tmp_path,
    )

    assert legal["ok"] is True
    assert calls[0][0] == [mod.LINEAR_AGENT, "status", "AGENTS-1", "Todo", "--assignee", "cto"]


def test_status_move_uses_idempotent_outbox_and_skips_repeated_op(mod, tmp_path):
    calls = []

    def runner(argv, stdin=None):
        calls.append((argv, stdin))
        return "moved"

    action = {"action": "status", "issue": "AGENTS-1", "from": "Backlog", "to": "Triage", "by": "cortex"}
    first = mod.execute_proposal(action, runner=runner, ledger_dir=tmp_path)
    second = mod.execute_proposal(action, runner=runner, ledger_dir=tmp_path)
    ledger = json.loads((tmp_path / "restricted-linear-writer-outbox.json").read_text(encoding="utf-8"))

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 1
    assert first["messages"][0].startswith("outbox committed team-os-board-move:")
    assert second["messages"][0].startswith("outbox already committed team-os-board-move:")
    assert len(ledger["operations"]) == 1
