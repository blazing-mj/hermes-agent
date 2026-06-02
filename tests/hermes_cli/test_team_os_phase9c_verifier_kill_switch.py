"""Verifier-output kill-switch slice — AGENTS-141.

RED/GREEN tests for the kill-switch guard in run_verification_plan and the
matching CLI wiring in verification-gate.
"""

from __future__ import annotations

import argparse
import json

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_plan(task_id: str = "TEST-1"):
    from hermes_cli.team_os.verification_gate import VerificationPlan
    return VerificationPlan(task_id=task_id, commands=(), requires_full_smoke=False)


# ---------------------------------------------------------------------------
# run_verification_plan — active kill switch
# ---------------------------------------------------------------------------

def test_active_kill_switch_returns_failed_report(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import VerificationStatus, run_verification_plan

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test halt")

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    assert report.status is VerificationStatus.FAILED
    assert report.can_close is False
    assert report.proof_artifact is None


def test_active_kill_switch_injects_single_kill_switch_command_result(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import run_verification_plan

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test halt")

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    assert len(report.commands) == 1
    assert report.commands[0].name == "kill-switch"
    assert report.commands[0].exit_code == 2


def test_active_kill_switch_output_is_json_with_enabled_status(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import run_verification_plan

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="halt for test")

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    status_data = json.loads(report.commands[0].output)
    assert status_data["enabled"] is True
    assert "halt for test" in status_data.get("reason", "")


def test_active_kill_switch_prevents_plan_commands_from_running(tmp_path):
    """Commands in the plan must not execute when kill-switch is armed."""
    import sys
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import VerificationCommand, VerificationPlan, run_verification_plan

    plan = VerificationPlan(
        task_id="TEST-2",
        commands=(
            VerificationCommand("echo-test", (sys.executable, "-c", "import sys; sys.exit(0)")),
        ),
        requires_full_smoke=False,
    )
    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="block")

    report = run_verification_plan(plan, kill_switch=ks)

    assert len(report.commands) == 1
    assert report.commands[0].name == "kill-switch"


# ---------------------------------------------------------------------------
# run_verification_plan — missing file / disabled / no kill switch
# ---------------------------------------------------------------------------

def test_missing_kill_switch_file_does_not_inject_kill_switch_command(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import run_verification_plan

    ks = KillSwitch(tmp_path / "nonexistent.json")  # file absent == disabled

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    assert not any(r.name == "kill-switch" for r in report.commands)


def test_none_kill_switch_does_not_inject_kill_switch_command():
    from hermes_cli.team_os.verification_gate import run_verification_plan

    report = run_verification_plan(_empty_plan(), kill_switch=None)

    assert not any(r.name == "kill-switch" for r in report.commands)


# ---------------------------------------------------------------------------
# run_verification_plan — corrupt state (fail closed)
# ---------------------------------------------------------------------------

def test_corrupt_kill_switch_file_fails_closed(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import VerificationStatus, run_verification_plan

    ks_file = tmp_path / "corrupt.json"
    ks_file.write_text("{ not valid json <<!!!", encoding="utf-8")
    ks = KillSwitch(ks_file)

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    assert report.status is VerificationStatus.FAILED
    assert report.can_close is False
    assert report.commands[0].name == "kill-switch"
    assert report.commands[0].exit_code == 2


def test_corrupt_kill_switch_output_records_source_corrupt(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import run_verification_plan

    ks_file = tmp_path / "corrupt.json"
    ks_file.write_text("[not an object]", encoding="utf-8")
    ks = KillSwitch(ks_file)

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    status_data = json.loads(report.commands[0].output)
    assert status_data["enabled"] is True
    assert status_data.get("source") == "corrupt"


# ---------------------------------------------------------------------------
# run_verification_plan — env-forced kill switch
# ---------------------------------------------------------------------------

def test_env_kill_switch_blocks_run(monkeypatch, tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.verification_gate import VerificationStatus, run_verification_plan

    monkeypatch.setenv("HERMES_TEAM_OS_KILL", "true")
    ks = KillSwitch(tmp_path / "ks.json")  # file absent; env forces enabled

    report = run_verification_plan(_empty_plan(), kill_switch=ks)

    assert report.status is VerificationStatus.FAILED
    assert report.commands[0].name == "kill-switch"


# ---------------------------------------------------------------------------
# CLI integration — verification-gate + kill-switch-state
# ---------------------------------------------------------------------------

def test_cli_verification_gate_with_active_kill_switch_returns_nonzero(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="CI halt")

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI",
        changed_file=[],
        test=[],
        output=None,
        plan_only=False,
        kill_switch_state=str(tmp_path / "ks.json"),
    )
    rc = cmd_team_os(args)
    assert rc != 0


def test_cli_verification_gate_with_active_kill_switch_output_has_failed_status(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test block")

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI",
        changed_file=[],
        test=[],
        output=None,
        plan_only=False,
        kill_switch_state=str(tmp_path / "ks.json"),
    )
    cmd_team_os(args)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "failed"
    assert data["can_close"] is False


def test_cli_verification_gate_with_active_kill_switch_commands_list_has_kill_switch(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test block")

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI",
        changed_file=[],
        test=[],
        output=None,
        plan_only=False,
        kill_switch_state=str(tmp_path / "ks.json"),
    )
    cmd_team_os(args)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert any(cmd["name"] == "kill-switch" for cmd in data["commands"])


def test_cli_verification_gate_with_corrupt_kill_switch_fails_closed(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    ks_file = tmp_path / "corrupt.json"
    ks_file.write_text("{not valid json", encoding="utf-8")

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI-CORRUPT",
        changed_file=[],
        test=[],
        output=None,
        plan_only=False,
        kill_switch_state=str(ks_file),
    )
    rc = cmd_team_os(args)
    assert rc == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    kill_switch_cmd = data["commands"][0]
    assert kill_switch_cmd["name"] == "kill-switch"
    status_data = json.loads(kill_switch_cmd["output"])
    assert status_data["enabled"] is True
    assert status_data.get("source") == "corrupt"


def test_cli_plan_only_ignores_active_kill_switch(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="should be ignored by plan-only")

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI",
        changed_file=[],
        test=[],
        output=None,
        plan_only=True,
        kill_switch_state=str(tmp_path / "ks.json"),
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    # Output must be valid JSON plan (not a failed report)
    data = json.loads(captured.out)
    assert "commands" in data
    assert "task_id" in data


def test_cli_verification_gate_proof_artifact_with_active_kill_switch(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="artifact test")
    output_file = tmp_path / "proof.json"

    args = argparse.Namespace(
        team_os_command="verification-gate",
        task_id="TEST-CLI",
        changed_file=[],
        test=[],
        output=str(output_file),
        plan_only=False,
        kill_switch_state=str(tmp_path / "ks.json"),
    )
    rc = cmd_team_os(args)

    assert rc != 0
    data = json.loads(output_file.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["can_close"] is False
    assert data["proof_artifact"] == str(output_file)
    assert any(cmd["name"] == "kill-switch" for cmd in data["commands"])
