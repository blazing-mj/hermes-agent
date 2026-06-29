"""Phase 6 active loop runner tests for hermes team-os.

Strict TDD: these specs are written before the active dispatcher exists.

Rules under test:
    * Active dispatch only runs against a SandboxWorkspace whose root is
      inside an explicitly allowed sandbox prefix. Pointing it at a
      production-style path (the project repo) raises
      ``SandboxBoundaryViolation`` and never spawns anything.
    * Only one worker may run at a time; the active dispatcher must hold
      the existing runner lock for the duration of the worker and release
      it on every exit path (success, failure, reclaim, timeout).
    * A successful sandbox worker yields ``status == "succeeded"``.
    * An intentionally failing worker yields ``status == "failed"`` and
      the resulting ``DispatchResult`` blocks the task
      (``blocks_task is True``).
    * A simulated crash (worker that stops emitting heartbeats while
      still alive) is reclaimed: the runner kills the worker, sets
      ``status == "reclaimed"`` and blocks the task.
    * A runaway worker that exceeds ``max_runtime_seconds`` is terminated
      and reported as ``status == "timeout"``.
    * Heartbeats observed by the dispatcher are recorded in the result.
    * The CLI surface (`loop-runner --active`) refuses to leave the
      sandbox and exits non-zero on failed/reclaimed dispatches.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest


# ---- Worker fixtures (no network, no real agent spawn) ----------------------

_SUCCESS_WORKER = textwrap.dedent(
    """
    import os, pathlib, sys, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    ws = pathlib.Path(os.environ["HERMES_SANDBOX_WORKSPACE"])
    assert ws.exists(), f"workspace missing: {ws}"
    for _ in range(3):
        hb.write_text(str(time.time()))
        time.sleep(0.05)
    (ws / "result.txt").write_text("ok")
    sys.exit(0)
    """
)

_ENV_PROBE_WORKER = textwrap.dedent(
    """
    import os, pathlib, sys, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    ws = pathlib.Path(os.environ["HERMES_SANDBOX_WORKSPACE"])
    leaked = [name for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LINEAR_API_KEY") if name in os.environ]
    hb.write_text(str(time.time()))
    (ws / "env-leaks.json").write_text("\\n".join(leaked))
    sys.exit(1 if leaked else 0)
    """
)

_FAILURE_WORKER = textwrap.dedent(
    """
    import os, pathlib, sys, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    hb.write_text(str(time.time()))
    time.sleep(0.05)
    hb.write_text(str(time.time()))
    sys.exit(7)
    """
)

# Simulated crash: writes one heartbeat then stops updating it while still
# alive. The runner must detect the stale heartbeat and reclaim.
_CRASH_WORKER = textwrap.dedent(
    """
    import os, pathlib, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    hb.write_text(str(time.time()))
    # Long sleep without ever updating the heartbeat — simulates a hung /
    # crashed worker that the dispatcher must reclaim.
    time.sleep(30)
    """
)

# Worker that updates heartbeats forever but never finishes — should trip the
# max-runtime guard rather than the stale-heartbeat guard.
_RUNAWAY_WORKER = textwrap.dedent(
    """
    import os, pathlib, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    end = time.time() + 30
    while time.time() < end:
        hb.write_text(str(time.time()))
        time.sleep(0.02)
    """
)


def _worker_cmd(script: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script)


def _task():
    from hermes_cli.team_os.loop_runner import LoopTask

    return LoopTask(
        task_id="AGENTS-73",
        title="phase6 sandbox proof",
        priority=10,
        status="ready",
        shifts=("day", "night"),
        approval_status="approved",
        quota_confidence="high",
    )


def _sandbox(tmp_path: Path, sub: str = "ws"):
    """Build a SandboxWorkspace rooted under tmp_path/sandbox/<sub>."""
    from hermes_cli.team_os.loop_runner import SandboxWorkspace

    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = sandbox_root / sub
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return SandboxWorkspace.create(workspace_dir, allowed_prefix=sandbox_root)


# ---- Sandbox boundary -------------------------------------------------------


def test_sandbox_workspace_refuses_path_outside_allowed_prefix(tmp_path):
    from hermes_cli.team_os.loop_runner import SandboxBoundaryViolation, SandboxWorkspace

    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    outside = tmp_path / "production"
    outside.mkdir()

    with pytest.raises(SandboxBoundaryViolation):
        SandboxWorkspace.create(outside, allowed_prefix=sandbox_root)


def test_sandbox_workspace_refuses_project_repo_paths(tmp_path):
    """The active dispatcher must refuse to operate on the hermes repo itself."""
    from hermes_cli.team_os.loop_runner import SandboxBoundaryViolation, SandboxWorkspace

    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    # The current working tree is a real Hermes checkout; pointing the
    # workspace at it must be refused even if allowed_prefix is permissive.
    repo_root = Path(__file__).resolve().parents[2]

    with pytest.raises(SandboxBoundaryViolation):
        SandboxWorkspace.create(repo_root, allowed_prefix=repo_root)


# ---- Proof: success path ----------------------------------------------------


def test_active_dispatch_runs_one_sandbox_task_to_success(tmp_path):
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _sandbox(tmp_path, sub="success")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_SUCCESS_WORKER),
        heartbeat_path=tmp_path / "heartbeat-success",
        lock_path=tmp_path / "loop-success.lock",
        owner="phase6-success",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.blocks_task is False
    assert result.heartbeats_observed >= 1
    assert result.task_id == "AGENTS-73"
    assert (Path(workspace.root) / "result.txt").read_text() == "ok"
    # Lock must be released on success.
    assert not (tmp_path / "loop-success.lock").exists()


def test_active_dispatch_does_not_pass_parent_api_secrets_to_worker(tmp_path, monkeypatch):
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("LINEAR_API_KEY", "should-not-leak")
    workspace = _sandbox(tmp_path, sub="env")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_ENV_PROBE_WORKER),
        heartbeat_path=tmp_path / "heartbeat-env",
        lock_path=tmp_path / "loop-env.lock",
        owner="phase6-env",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    assert result.status == "succeeded"
    assert (Path(workspace.root) / "env-leaks.json").read_text() == ""


# ---- Failure blocks task ----------------------------------------------------


def test_active_dispatch_failed_worker_blocks_task(tmp_path):
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _sandbox(tmp_path, sub="failure")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_FAILURE_WORKER),
        heartbeat_path=tmp_path / "heartbeat-failure",
        lock_path=tmp_path / "loop-failure.lock",
        owner="phase6-failure",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.blocks_task is True
    assert "exit" in result.reason.lower() or "fail" in result.reason.lower()
    assert not (tmp_path / "loop-failure.lock").exists()


# ---- Proof: crash gets reclaimed -------------------------------------------


def test_active_dispatch_reclaims_crashed_worker(tmp_path):
    """A worker that stops emitting heartbeats while alive is reclaimed."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _sandbox(tmp_path, sub="crash")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_CRASH_WORKER),
        heartbeat_path=tmp_path / "heartbeat-crash",
        lock_path=tmp_path / "loop-crash.lock",
        owner="phase6-crash",
        max_runtime_seconds=10.0,
        heartbeat_stale_seconds=0.25,
        poll_interval=0.02,
    )

    assert result.status == "reclaimed"
    assert result.blocks_task is True
    assert "heartbeat" in result.reason.lower() or "stale" in result.reason.lower()
    # The runner must have killed the worker; lock released.
    assert not (tmp_path / "loop-crash.lock").exists()


# ---- Max runtime ------------------------------------------------------------


def test_active_dispatch_kills_worker_exceeding_max_runtime(tmp_path):
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _sandbox(tmp_path, sub="runaway")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_RUNAWAY_WORKER),
        heartbeat_path=tmp_path / "heartbeat-runaway",
        lock_path=tmp_path / "loop-runaway.lock",
        owner="phase6-runaway",
        max_runtime_seconds=0.3,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
    )

    assert result.status == "timeout"
    assert result.blocks_task is True
    assert "runtime" in result.reason.lower() or "timeout" in result.reason.lower()


# ---- Single worker exclusivity ---------------------------------------------


def test_active_dispatch_refuses_when_lock_already_held(tmp_path):
    from hermes_cli.team_os.loop_runner import (
        RunnerAlreadyActive,
        acquire_runner_lock,
        run_active_dispatch,
    )

    lock_path = tmp_path / "loop-exclusive.lock"
    held = acquire_runner_lock(lock_path, owner="other-runner")
    try:
        workspace = _sandbox(tmp_path, sub="exclusive")
        with pytest.raises(RunnerAlreadyActive):
            run_active_dispatch(
                _task(),
                workspace=workspace,
                worker_command=_worker_cmd(_SUCCESS_WORKER),
                heartbeat_path=tmp_path / "heartbeat-exclusive",
                lock_path=lock_path,
                owner="phase6-exclusive",
                max_runtime_seconds=5.0,
                heartbeat_stale_seconds=1.0,
                poll_interval=0.02,
            )
    finally:
        held.release()


# ---- Result serialisation --------------------------------------------------


def test_dispatch_result_serialises_for_logging(tmp_path):
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _sandbox(tmp_path, sub="serialise")
    result = run_active_dispatch(
        _task(),
        workspace=workspace,
        worker_command=_worker_cmd(_SUCCESS_WORKER),
        heartbeat_path=tmp_path / "heartbeat-serialise",
        lock_path=tmp_path / "loop-serialise.lock",
        owner="phase6-serialise",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    data = result.to_dict()
    assert data["task_id"] == "AGENTS-73"
    assert data["status"] == "succeeded"
    assert data["blocks_task"] is False
    assert data["dry_run"] is False
    assert data["workspace"].endswith("serialise")
    assert "heartbeats_observed" in data


# ---- CLI surface -----------------------------------------------------------


def test_cli_loop_runner_active_success_path(tmp_path):
    from argparse import Namespace

    from hermes_cli.team_os.cli import cmd_team_os

    sandbox_root = tmp_path / "sandbox"
    workspace_dir = sandbox_root / "cli-success"
    workspace_dir.mkdir(parents=True)

    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            [
                {
                    "task_id": "AGENTS-73",
                    "title": "phase6 sandbox proof",
                    "priority": 10,
                    "status": "ready",
                    "shifts": ["day", "night"],
                    "approval_status": "approved",
                    "quota_confidence": "high",
                }
            ]
        )
    )
    output = tmp_path / "dispatch.json"
    args = Namespace(
        team_os_command="loop-runner",
        tasks=str(tasks_path),
        shift="day",
        output=str(output),
        lock=str(tmp_path / "cli-success.lock"),
        owner="phase6-cli-success",
        active=True,
        # hermetic: don't read the machine's real kill-switch (TeamOS may be paused)
        kill_switch_state=str(tmp_path / "kill-switch.json"),
        sandbox_root=str(sandbox_root),
        workspace=str(workspace_dir),
        worker_cmd=[sys.executable, "-c", _SUCCESS_WORKER],
        heartbeat_path=str(tmp_path / "cli-success-hb"),
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    rc = cmd_team_os(args)

    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["status"] == "succeeded"
    assert data["task_id"] == "AGENTS-73"
    assert data["dry_run"] is False


def test_cli_loop_runner_active_refuses_workspace_outside_sandbox(tmp_path):
    from argparse import Namespace

    from hermes_cli.team_os.cli import cmd_team_os

    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    outside = tmp_path / "production"
    outside.mkdir()
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            [
                {
                    "task_id": "AGENTS-73",
                    "title": "phase6 sandbox proof",
                    "priority": 10,
                    "status": "ready",
                    "shifts": ["day"],
                    "approval_status": "approved",
                    "quota_confidence": "high",
                }
            ]
        )
    )
    args = Namespace(
        team_os_command="loop-runner",
        tasks=str(tasks_path),
        shift="day",
        output=None,
        lock=str(tmp_path / "boundary.lock"),
        owner="phase6-boundary",
        active=True,
        sandbox_root=str(sandbox_root),
        workspace=str(outside),
        worker_cmd=[sys.executable, "-c", _SUCCESS_WORKER],
        heartbeat_path=str(tmp_path / "boundary-hb"),
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    rc = cmd_team_os(args)
    assert rc != 0


def test_cli_loop_runner_active_failure_returns_non_zero(tmp_path):
    from argparse import Namespace

    from hermes_cli.team_os.cli import cmd_team_os

    sandbox_root = tmp_path / "sandbox"
    workspace_dir = sandbox_root / "cli-failure"
    workspace_dir.mkdir(parents=True)
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            [
                {
                    "task_id": "AGENTS-73",
                    "title": "phase6 sandbox proof",
                    "priority": 10,
                    "status": "ready",
                    "shifts": ["day"],
                    "approval_status": "approved",
                    "quota_confidence": "high",
                }
            ]
        )
    )
    output = tmp_path / "dispatch-failure.json"
    args = Namespace(
        team_os_command="loop-runner",
        tasks=str(tasks_path),
        shift="day",
        output=str(output),
        lock=str(tmp_path / "cli-failure.lock"),
        owner="phase6-cli-failure",
        active=True,
        # hermetic: don't read the machine's real kill-switch (TeamOS may be paused)
        kill_switch_state=str(tmp_path / "kill-switch.json"),
        sandbox_root=str(sandbox_root),
        workspace=str(workspace_dir),
        worker_cmd=[sys.executable, "-c", _FAILURE_WORKER],
        heartbeat_path=str(tmp_path / "cli-failure-hb"),
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.02,
    )

    rc = cmd_team_os(args)
    assert rc != 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["blocks_task"] is True
