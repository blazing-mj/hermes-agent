"""Phase 9: Team OS production-mode execution gate tests.

Strict TDD — tests written BEFORE implementation.

Scope:
  * ProductionGate: pure data check (kill-switch off, approval, high confidence).
  * Audit trail: write_production_audit appends JSONL with required fields.
  * select_next_task: production_mode=True implies require_approval + require_confidence.
  * run_active_dispatch: production_mode=True runs gate before dispatch;
    fails closed on any violation; writes audit trail on success.
  * Sandbox default (production_mode=False) never writes audit trail.
  * CLI: --production-mode flag wires through correctly.

No network access, no real agent spawns.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    *,
    task_id: str = "T1",
    approval_status: str | None = "approved",
    task_confidence: str | None = "high",
    quota_confidence: str = "high",
    status: str = "ready",
    shifts: tuple[str, ...] = ("day",),
    priority: int = 1,
):
    from hermes_cli.team_os.loop_runner import LoopTask
    return LoopTask(
        task_id=task_id,
        title=f"task-{task_id}",
        priority=priority,
        status=status,
        shifts=shifts,
        approval_status=approval_status,
        quota_confidence=quota_confidence,
        task_confidence=task_confidence,
    )


# ---------------------------------------------------------------------------
# ProductionGate unit tests
# ---------------------------------------------------------------------------


def test_production_gate_passes_when_all_conditions_met(tmp_path):
    """All gates clear → ProductionGateResult.passed is True."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")  # disabled by default
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is True
    assert result.violations == ()


def test_production_gate_passes_auto_approved(tmp_path):
    """auto-approved is a valid approval status for production."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="auto-approved", task_confidence="high", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is True


def test_production_gate_fails_when_kill_switch_enabled(tmp_path):
    """Kill-switch on → gate fails with kill-switch violation."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="halt")
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("kill-switch" in v for v in result.violations)


def test_production_gate_fails_when_approval_is_none(tmp_path):
    """No approval recorded → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status=None, task_confidence="high", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("approval_status" in v for v in result.violations)


def test_production_gate_fails_when_approval_rejected(tmp_path):
    """Rejected approval → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="rejected", task_confidence="high", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("approval_status" in v for v in result.violations)


def test_production_gate_fails_when_task_confidence_low(tmp_path):
    """task_confidence=low → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="approved", task_confidence="low", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("task_confidence" in v for v in result.violations)


def test_production_gate_fails_when_task_confidence_is_none(tmp_path):
    """task_confidence=None (not assessed) → gate fails in production mode."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="approved", task_confidence=None, quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("task_confidence" in v for v in result.violations)


def test_production_gate_fails_when_task_confidence_unknown(tmp_path):
    """task_confidence=unknown → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="approved", task_confidence="unknown", quota_confidence="high")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False


def test_production_gate_fails_when_quota_confidence_unknown(tmp_path):
    """quota_confidence=unknown → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="unknown")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    assert any("quota_confidence" in v for v in result.violations)


def test_production_gate_fails_when_quota_confidence_exhausted(tmp_path):
    """quota_confidence=exhausted → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="exhausted")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False


def test_production_gate_accumulates_all_violations(tmp_path):
    """Multiple failing conditions → all violations are reported."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.production_gate import check_production_gate

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="multi-violation test")
    # No approval, low task confidence, blocking quota confidence
    task = _make_task(approval_status=None, task_confidence="low", quota_confidence="unknown")

    result = check_production_gate(task, kill_switch=ks)

    assert result.passed is False
    # Should have at least 3 violations (kill-switch, approval, task confidence, quota)
    assert len(result.violations) >= 3


def test_production_gate_result_to_dict():
    """to_dict() returns serializable structure."""
    from hermes_cli.team_os.production_gate import ProductionGateResult

    result = ProductionGateResult(passed=False, violations=("kill-switch is enabled",))
    d = result.to_dict()
    assert d["passed"] is False
    assert "kill-switch is enabled" in d["violations"]
    # Must be JSON-serializable
    json.dumps(d)


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------


def test_write_production_audit_creates_file(tmp_path):
    """write_production_audit creates the JSONL file on first write."""
    from hermes_cli.team_os.production_gate import write_production_audit

    audit_path = tmp_path / "sub" / "audit.jsonl"
    write_production_audit(
        task_id="T1",
        task_title="test task",
        owner="runner-1",
        approval_status="approved",
        task_confidence="high",
        quota_confidence="high",
        workspace=str(tmp_path / "ws"),
        audit_path=audit_path,
    )

    assert audit_path.exists()


def test_write_production_audit_entry_has_required_fields(tmp_path):
    """Each entry has ts, task_id, task_title, owner, approval_status,
    task_confidence, quota_confidence, workspace."""
    from hermes_cli.team_os.production_gate import write_production_audit

    audit_path = tmp_path / "audit.jsonl"
    write_production_audit(
        task_id="T-audit",
        task_title="my task",
        owner="runner-x",
        approval_status="auto-approved",
        task_confidence="high",
        quota_confidence="high",
        workspace="/tmp/ws",
        audit_path=audit_path,
    )

    entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert entry["task_id"] == "T-audit"
    assert entry["task_title"] == "my task"
    assert entry["owner"] == "runner-x"
    assert entry["approval_status"] == "auto-approved"
    assert entry["task_confidence"] == "high"
    assert entry["quota_confidence"] == "high"
    assert entry["workspace"] == "/tmp/ws"
    assert "ts" in entry


def test_write_production_audit_appends_multiple_entries(tmp_path):
    """Multiple calls append separate JSONL lines."""
    from hermes_cli.team_os.production_gate import write_production_audit

    audit_path = tmp_path / "audit.jsonl"
    for i in range(3):
        write_production_audit(
            task_id=f"T{i}",
            task_title=f"task-{i}",
            owner="runner",
            approval_status="approved",
            task_confidence="high",
            quota_confidence="high",
            workspace="/tmp/ws",
            audit_path=audit_path,
        )

    lines = [l for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    ids = {json.loads(l)["task_id"] for l in lines}
    assert ids == {"T0", "T1", "T2"}


# ---------------------------------------------------------------------------
# select_next_task production_mode tests
# ---------------------------------------------------------------------------


def test_select_next_task_production_mode_blocks_without_explicit_approval():
    """production_mode=True blocks tasks where approval_status=None."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status=None, task_confidence="high")
    decision = select_next_task([task], current_shift="day", production_mode=True)

    assert decision.selected_task_id is None
    assert "T1" in decision.skip_reasons


def test_select_next_task_production_mode_blocks_unassessed_confidence():
    """production_mode=True blocks tasks with task_confidence=None."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status="approved", task_confidence=None)
    decision = select_next_task([task], current_shift="day", production_mode=True)

    assert decision.selected_task_id is None
    assert "T1" in decision.skip_reasons


def test_select_next_task_production_mode_blocks_low_confidence():
    """production_mode=True blocks tasks with task_confidence=low."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status="approved", task_confidence="low")
    decision = select_next_task([task], current_shift="day", production_mode=True)

    assert decision.selected_task_id is None
    assert "T1" in decision.skip_reasons


def test_select_next_task_production_mode_passes_fully_ready_task():
    """production_mode=True selects a task that has approval + high confidence."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")
    decision = select_next_task([task], current_shift="day", production_mode=True)

    assert decision.selected_task_id == "T1"


def test_select_next_task_production_mode_false_is_backward_compat():
    """production_mode=False (default) does not add extra gates."""
    from hermes_cli.team_os.loop_runner import select_next_task

    # Backward compat: approval_status=None + task_confidence=None pass in default mode
    task = _make_task(approval_status=None, task_confidence=None, quota_confidence="high")
    decision = select_next_task([task], current_shift="day")

    assert decision.selected_task_id == "T1"


def test_select_next_task_production_mode_decision_carries_flag():
    """LoopDecision.production_mode reflects the mode the runner was called with."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status="approved", task_confidence="high")
    decision = select_next_task([task], current_shift="day", production_mode=True)

    assert decision.production_mode is True


def test_select_next_task_sandbox_decision_carries_flag():
    """LoopDecision.production_mode is False for sandbox calls."""
    from hermes_cli.team_os.loop_runner import select_next_task

    task = _make_task(approval_status="approved", task_confidence="high")
    decision = select_next_task([task], current_shift="day", production_mode=False)

    assert decision.production_mode is False


# ---------------------------------------------------------------------------
# run_active_dispatch production_mode tests
# ---------------------------------------------------------------------------


def _simple_workspace(tmp_path):
    from hermes_cli.team_os.loop_runner import SandboxWorkspace
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    return SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)


def test_run_active_dispatch_production_gate_blocks_kill_switch_enabled(tmp_path):
    """Production dispatch with kill-switch on → DispatchResult blocks_task=True."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="production gate test")
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-prod-ks",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=True,
    )

    assert result.blocks_task is True
    assert result.status in {"aborted", "production_gate_failed"}
    assert "kill-switch" in result.reason
    assert result.production_mode is True


def test_run_active_dispatch_production_gate_blocks_no_approval(tmp_path):
    """Production dispatch without approval → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")  # disabled
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status=None, task_confidence="high", quota_confidence="high")

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-no-approval",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=True,
    )

    assert result.blocks_task is True
    assert result.status in {"aborted", "production_gate_failed"}
    assert result.production_mode is True


def test_run_active_dispatch_production_gate_blocks_low_confidence(tmp_path):
    """Production dispatch with low task confidence → gate fails."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="low", quota_confidence="high")

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-low-conf",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=True,
    )

    assert result.blocks_task is True
    assert result.production_mode is True


def test_run_active_dispatch_production_mode_requires_kill_switch(tmp_path):
    """production_mode=True with kill_switch=None → gate fails closed."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-no-ks",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=None,
        production_mode=True,
    )

    assert result.blocks_task is True
    assert "kill-switch" in result.reason.lower()


def test_run_active_dispatch_production_mode_succeeds_and_writes_audit(tmp_path):
    """Production dispatch with all gates passing runs worker + writes audit trail."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")  # disabled
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")
    audit_path = tmp_path / "audit.jsonl"

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-prod-ok",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=True,
        audit_path=audit_path,
    )

    assert result.status == "succeeded"
    assert result.production_mode is True
    # Audit trail must have been written
    assert audit_path.exists(), "audit trail must be written for production dispatch"
    entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert entry["task_id"] == "T1"
    assert entry["owner"] == "test-prod-ok"
    assert entry["approval_status"] == "approved"


def test_run_active_dispatch_sandbox_does_not_write_audit(tmp_path):
    """Sandbox dispatch (production_mode=False) MUST NOT write the audit trail.

    This test proves there is no accidental production execution in default mode.
    """
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")
    audit_path = tmp_path / "audit.jsonl"

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-sandbox",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=False,  # explicit sandbox
        audit_path=audit_path,
    )

    assert result.status == "succeeded"
    assert result.production_mode is False
    # Sandbox run must NOT produce an audit trail
    assert not audit_path.exists(), "sandbox run must NOT write the production audit trail"


def test_run_active_dispatch_result_production_mode_false_by_default(tmp_path):
    """DispatchResult.production_mode defaults to False (backward compat)."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ws = _simple_workspace(tmp_path)
    task = _make_task()

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-default",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
    )

    assert result.production_mode is False


def test_dispatch_result_to_dict_includes_production_mode(tmp_path):
    """DispatchResult.to_dict() includes production_mode key."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    ws = _simple_workspace(tmp_path)
    task = _make_task(approval_status="approved", task_confidence="high", quota_confidence="high")

    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test-dict",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        kill_switch=ks,
        production_mode=True,
        audit_path=tmp_path / "audit.jsonl",
    )

    d = result.to_dict()
    assert "production_mode" in d
    assert d["production_mode"] is True
    json.dumps(d)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# No accidental production execution: sandbox can never write audit trail
# ---------------------------------------------------------------------------


def test_sandbox_mode_is_default_and_cannot_write_audit_even_if_path_given(tmp_path):
    """Even when audit_path is passed, sandbox runs must not write to it."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ws = _simple_workspace(tmp_path)
    task = _make_task()
    audit_path = tmp_path / "should_not_exist.jsonl"

    run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="test",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        # No production_mode=True — default sandbox
        audit_path=audit_path,
    )

    assert not audit_path.exists(), "audit_path must not be written in sandbox mode"


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_cli_loop_runner_production_mode_flag_exists():
    """The --production-mode flag must be registered on the loop-runner subcommand."""
    import argparse
    from hermes_cli.team_os.cli import register_cli

    parser = argparse.ArgumentParser()
    team_os_sub = parser.add_subparsers(dest="cmd")
    team_os_parser = team_os_sub.add_parser("team-os")
    register_cli(team_os_parser)

    # Parse with --production-mode present — must not raise
    args = parser.parse_args([
        "team-os", "loop-runner",
        "--tasks", "/tmp/tasks.json",
        "--production-mode",
    ])
    assert getattr(args, "production_mode", False) is True


def test_cli_loop_runner_without_production_mode_defaults_false():
    """Without --production-mode the flag defaults to False."""
    import argparse
    from hermes_cli.team_os.cli import register_cli

    parser = argparse.ArgumentParser()
    team_os_sub = parser.add_subparsers(dest="cmd")
    team_os_parser = team_os_sub.add_parser("team-os")
    register_cli(team_os_parser)

    args = parser.parse_args(["team-os", "loop-runner", "--tasks", "/tmp/tasks.json"])
    assert getattr(args, "production_mode", False) is False


def test_cli_loop_runner_production_mode_audit_path_arg():
    """--audit-path flag must be registered for production audit trail."""
    import argparse
    from hermes_cli.team_os.cli import register_cli

    parser = argparse.ArgumentParser()
    team_os_sub = parser.add_subparsers(dest="cmd")
    team_os_parser = team_os_sub.add_parser("team-os")
    register_cli(team_os_parser)

    args = parser.parse_args([
        "team-os", "loop-runner",
        "--tasks", "/tmp/tasks.json",
        "--production-mode",
        "--audit-path", "/tmp/audit.jsonl",
    ])
    assert getattr(args, "audit_path", None) == "/tmp/audit.jsonl"
