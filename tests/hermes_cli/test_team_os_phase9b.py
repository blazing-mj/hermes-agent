"""Phase 9B (AGENTS-78): CLI production-mode gate tests.

Strict TDD — tests written BEFORE Phase 9B implementation.

Scope:
  * CLI loop-runner active path supports --production and --production-audit.
  * --production without --active returns rc=2.
  * Denied production (no approval, low confidence, kill-switch on, …) returns
    rc=2, writes a denial audit row, does NOT acquire the lock, and does NOT
    call active dispatch.
  * Allowed production runs the existing sandbox-bounded active dispatch and
    stamps the JSON output with mode="production".
  * Default (no --production) is sandbox; no audit row is written even when
    --production-audit is set.

These tests drive the cmd_team_os entry point directly with a parsed argparse
namespace so they exercise the CLI wiring end-to-end without spawning a
subprocess.  All worker_command argv passed to the sandbox is a trivial
`python -c "pass"` exit-0 process; nothing in this file reaches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_parser():
    from hermes_cli.team_os.cli import register_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    team_os_parser = sub.add_parser("team-os")
    register_cli(team_os_parser)
    return parser


def _write_tasks(path: Path, tasks: list[dict]) -> Path:
    path.write_text(json.dumps(tasks), encoding="utf-8")
    return path


def _approved_task() -> dict:
    return {
        "task_id": "T1",
        "title": "approved high-confidence task",
        "priority": 1,
        "status": "ready",
        "shifts": ["day", "night"],
        "approval_status": "approved",
        "quota_confidence": "high",
        "task_confidence": "high",
    }


def _no_approval_task() -> dict:
    t = _approved_task()
    t["approval_status"] = None
    return t


def _low_confidence_task() -> dict:
    t = _approved_task()
    t["task_confidence"] = "low"
    return t


def _common_active_argv(
    *,
    tasks: Path,
    sandbox_root: Path,
    workspace: Path,
    heartbeat: Path,
    lock: Path,
    ks_state: Path,
) -> list[str]:
    """Argv stub for the loop-runner --active path.

    NOTE: ``--worker-cmd`` is intentionally omitted because argparse's
    ``nargs="+"`` cannot cleanly carry ``-c pass`` (it parses ``-c`` as a
    flag).  Tests inject ``args.worker_cmd`` directly after parsing.
    """
    return [
        "team-os",
        "loop-runner",
        "--tasks",
        str(tasks),
        "--active",
        "--sandbox-root",
        str(sandbox_root),
        "--workspace",
        str(workspace),
        "--heartbeat-path",
        str(heartbeat),
        "--lock",
        str(lock),
        "--kill-switch-state",
        str(ks_state),
        "--max-runtime-seconds",
        "5",
        "--heartbeat-stale-seconds",
        "5",
        "--poll-interval",
        "0.05",
    ]


def _inject_worker_cmd(args) -> None:
    args.worker_cmd = [sys.executable, "-c", "pass"]


# ---------------------------------------------------------------------------
# CLI flag wiring
# ---------------------------------------------------------------------------


def test_cli_production_flag_parses(tmp_path):
    parser = _build_parser()
    args = parser.parse_args(
        [
            "team-os",
            "loop-runner",
            "--tasks",
            str(tmp_path / "tasks.json"),
            "--production",
        ]
    )
    assert getattr(args, "production", False) is True


def test_cli_production_audit_flag_parses(tmp_path):
    parser = _build_parser()
    args = parser.parse_args(
        [
            "team-os",
            "loop-runner",
            "--tasks",
            str(tmp_path / "tasks.json"),
            "--production",
            "--production-audit",
            str(tmp_path / "a.jsonl"),
        ]
    )
    assert getattr(args, "production_audit", None) == str(tmp_path / "a.jsonl")


def test_cli_production_default_false(tmp_path):
    parser = _build_parser()
    args = parser.parse_args(
        [
            "team-os",
            "loop-runner",
            "--tasks",
            str(tmp_path / "tasks.json"),
        ]
    )
    assert getattr(args, "production", False) is False


# ---------------------------------------------------------------------------
# --production without --active
# ---------------------------------------------------------------------------


def test_production_without_active_errors_rc2(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_approved_task()])

    parser = _build_parser()
    args = parser.parse_args(
        [
            "team-os",
            "loop-runner",
            "--tasks",
            str(tasks_path),
            "--production",
        ]
    )
    rc = cmd_team_os(args)
    assert rc == 2
    # message must mention --active so operator knows what to do
    captured = capsys.readouterr()
    assert "active" in (captured.out + captured.err).lower()


# ---------------------------------------------------------------------------
# Denied production: rc=2, audit row written, no lock, no dispatch
# ---------------------------------------------------------------------------


def test_production_denied_no_approval_rc2_audit_no_lock_no_dispatch(tmp_path, monkeypatch):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os import loop_runner as lr

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_no_approval_task()])
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    audit = tmp_path / "audit.jsonl"
    lock = tmp_path / "lock"
    ks_state = tmp_path / "ks.json"

    # Spy: dispatch must NOT be called on a denial.
    called = {"count": 0}

    def _boom(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("run_active_dispatch must not be called when production gate denies")

    monkeypatch.setattr(lr, "run_active_dispatch", _boom)

    parser = _build_parser()
    argv = _common_active_argv(
        tasks=tasks_path,
        sandbox_root=tmp_path,
        workspace=sandbox,
        heartbeat=tmp_path / "hb",
        lock=lock,
        ks_state=ks_state,
    ) + ["--production", "--production-audit", str(audit)]
    args = parser.parse_args(argv)
    _inject_worker_cmd(args)

    rc = cmd_team_os(args)

    assert rc == 2
    assert called["count"] == 0, "active dispatch must not run on production-gate denial"
    assert audit.exists(), "denial audit row must be written"
    entry = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert entry["task_id"] == "T1"
    # Phase 9B audit row records the denial decision.
    assert entry.get("decision") == "denied"
    assert "violations" in entry and entry["violations"]
    assert not lock.exists(), "loop-runner lock must not be acquired on denial"


def test_production_denied_kill_switch_rc2_and_audit(tmp_path, monkeypatch):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os import loop_runner as lr
    from hermes_cli.team_os.kill_switch import KillSwitch

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_approved_task()])
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    audit = tmp_path / "audit.jsonl"
    lock = tmp_path / "lock"
    ks_state = tmp_path / "ks.json"

    KillSwitch(ks_state).enable(reason="phase9b halt test")

    called = {"count": 0}

    def _boom(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("dispatch must not be called when kill-switch denies production")

    monkeypatch.setattr(lr, "run_active_dispatch", _boom)

    parser = _build_parser()
    argv = _common_active_argv(
        tasks=tasks_path,
        sandbox_root=tmp_path,
        workspace=sandbox,
        heartbeat=tmp_path / "hb",
        lock=lock,
        ks_state=ks_state,
    ) + ["--production", "--production-audit", str(audit)]
    args = parser.parse_args(argv)
    _inject_worker_cmd(args)

    rc = cmd_team_os(args)

    assert rc == 2
    assert called["count"] == 0
    assert audit.exists()
    entry = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert entry.get("decision") == "denied"
    joined = " ".join(entry.get("violations") or [])
    assert "kill-switch" in joined.lower()
    assert not lock.exists()


def test_production_denied_low_confidence_rc2_and_audit(tmp_path, monkeypatch):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os import loop_runner as lr

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_low_confidence_task()])
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    audit = tmp_path / "audit.jsonl"
    lock = tmp_path / "lock"
    ks_state = tmp_path / "ks.json"

    monkeypatch.setattr(
        lr,
        "run_active_dispatch",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("dispatch must not be called when task_confidence is low")
        ),
    )

    parser = _build_parser()
    argv = _common_active_argv(
        tasks=tasks_path,
        sandbox_root=tmp_path,
        workspace=sandbox,
        heartbeat=tmp_path / "hb",
        lock=lock,
        ks_state=ks_state,
    ) + ["--production", "--production-audit", str(audit)]
    args = parser.parse_args(argv)
    _inject_worker_cmd(args)

    rc = cmd_team_os(args)

    assert rc == 2
    assert audit.exists()
    entry = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert entry.get("decision") == "denied"
    assert not lock.exists()


# ---------------------------------------------------------------------------
# Allowed production: still sandbox-bounded, stamps mode=production
# ---------------------------------------------------------------------------


def test_production_allowed_dispatches_and_stamps_mode_production(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_approved_task()])
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    audit = tmp_path / "audit.jsonl"
    lock = tmp_path / "lock"
    ks_state = tmp_path / "ks.json"

    parser = _build_parser()
    argv = _common_active_argv(
        tasks=tasks_path,
        sandbox_root=tmp_path,
        workspace=sandbox,
        heartbeat=tmp_path / "hb",
        lock=lock,
        ks_state=ks_state,
    ) + ["--production", "--production-audit", str(audit)]
    args = parser.parse_args(argv)
    _inject_worker_cmd(args)

    rc = cmd_team_os(args)
    captured = capsys.readouterr()

    assert rc == 0, captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload.get("mode") == "production"
    # The underlying dispatch result is still sandbox-bounded — same workspace.
    assert audit.exists(), "allowed production runs write a success audit row"
    # The audit row for success records the same task + approval.
    last = audit.read_text(encoding="utf-8").splitlines()[-1]
    entry = json.loads(last)
    assert entry["task_id"] == "T1"
    assert entry["approval_status"] == "approved"


# ---------------------------------------------------------------------------
# Default sandbox: no audit even if --production-audit is set
# ---------------------------------------------------------------------------


def test_default_sandbox_active_does_not_write_audit_or_stamp_mode_production(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    tasks_path = _write_tasks(tmp_path / "tasks.json", [_approved_task()])
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    audit = tmp_path / "audit.jsonl"
    lock = tmp_path / "lock"
    ks_state = tmp_path / "ks.json"

    parser = _build_parser()
    # No --production; --production-audit path supplied but must be ignored.
    argv = _common_active_argv(
        tasks=tasks_path,
        sandbox_root=tmp_path,
        workspace=sandbox,
        heartbeat=tmp_path / "hb",
        lock=lock,
        ks_state=ks_state,
    ) + ["--production-audit", str(audit)]
    args = parser.parse_args(argv)
    _inject_worker_cmd(args)

    rc = cmd_team_os(args)
    captured = capsys.readouterr()

    assert rc == 0, captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload.get("mode") != "production"
    assert not audit.exists(), "default sandbox mode must not write the production audit"
