"""Phase 10 (AGENTS-79): worker isolation hardening.

Strict TDD — written BEFORE Phase 10 implementation lands.

Scope (from approved AGENTS-79 audit + spec):

  * Process-group reaping.  The active dispatcher must terminate child AND
    grandchild workers on every non-success terminal path:
      - max-runtime timeout
      - stale heartbeat reclaim
      - mid-run kill-switch abort
    To guarantee this it must start the worker in a fresh process group
    (``start_new_session=True`` on POSIX) and use ``os.killpg`` as the
    termination fallback.

  * Opt-in WorkerResourceLimits dataclass.  Defaults are off, so existing
    callers (Phase 6/9/9A/9B) see no behavior change.  When provided,
    limits are applied via ``preexec_fn`` so they are inherited by the
    worker and its grandchildren.  The reliable test target on macOS is
    RLIMIT_FSIZE; flakier limits are covered with platform skip-guards.

  * Fail-closed production audit guard.  ``run_active_dispatch`` invoked
    with ``production_mode=True`` and ``audit_path=None`` must return a
    DispatchResult with ``status='production_audit_required'`` and
    ``blocks_task=True`` BEFORE acquiring the runner lock or spawning a
    worker.  The CLI already supplies a default audit path, so the CLI
    surface is unaffected.

No network access.  All workers are local ``python -c`` stubs that touch
files inside the sandbox workspace; nothing here reaches an agent runtime.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import textwrap
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
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


def _simple_workspace(tmp_path: Path):
    from hermes_cli.team_os.loop_runner import SandboxWorkspace

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return SandboxWorkspace.create(ws, allowed_prefix=tmp_path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_dead(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


# ---------------------------------------------------------------------------
# Process-group reaping
# ---------------------------------------------------------------------------


# Parent worker that:
#   * spawns a grandchild that ticks an "alive" file forever, NOT in a new
#     session (so it inherits the parent's process group);
#   * writes the grandchild pid to disk;
#   * ticks the heartbeat itself so the parent does not trip stale-heartbeat
#     reclaim before we exercise the max-runtime / kill-switch guard.
_GRANDCHILD_PARENT_HB_TICKING = textwrap.dedent(
    """
    import os, pathlib, subprocess, sys, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    ws = pathlib.Path(os.environ["HERMES_SANDBOX_WORKSPACE"])
    grandchild_code = (
        "import pathlib, time;"
        f"alive=pathlib.Path({str(ws / 'grandchild.alive')!r});"
        "[(alive.write_text(str(time.time())), time.sleep(0.05)) for _ in range(2400)]"
    )
    # IMPORTANT: do NOT start_new_session here — we want the grandchild to
    # share the parent's process group so a killpg reaches it.
    proc = subprocess.Popen([sys.executable, "-c", grandchild_code])
    (ws / "grandchild.pid").write_text(str(proc.pid))
    end = time.time() + 120
    while time.time() < end:
        hb.write_text(str(time.time()))
        time.sleep(0.02)
    """
)


# Parent worker that spawns a grandchild then immediately STOPS ticking the
# heartbeat.  Used to prove the stale-heartbeat reclaim path also reaps the
# grandchild.
_GRANDCHILD_PARENT_STALE_HB = textwrap.dedent(
    """
    import os, pathlib, subprocess, sys, time
    hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    ws = pathlib.Path(os.environ["HERMES_SANDBOX_WORKSPACE"])
    grandchild_code = (
        "import pathlib, time;"
        f"alive=pathlib.Path({str(ws / 'grandchild.alive')!r});"
        "[(alive.write_text(str(time.time())), time.sleep(0.05)) for _ in range(2400)]"
    )
    proc = subprocess.Popen([sys.executable, "-c", grandchild_code])
    (ws / "grandchild.pid").write_text(str(proc.pid))
    # Write exactly one heartbeat then go silent so the dispatcher's stale
    # heartbeat reclaim path fires.
    hb.write_text(str(time.time()))
    time.sleep(60)
    """
)


def _read_grandchild_pid(workspace_root: Path) -> int:
    pid_file = workspace_root / "grandchild.pid"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            try:
                return int(pid_file.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.02)
    raise AssertionError(f"grandchild pid file never appeared: {pid_file}")


def _assert_grandchild_reaped(workspace_root: Path, grandchild_pid: int) -> None:
    """Best-effort verification that the grandchild process is dead.

    We check both:
      - the PID is no longer alive (os.kill(pid, 0)); and
      - the alive-file mtime stops advancing (defence against PID reuse).
    """
    alive_file = workspace_root / "grandchild.alive"
    assert _wait_for_pid_dead(grandchild_pid, timeout=3.0), (
        f"grandchild pid {grandchild_pid} survived dispatch — process group not reaped"
    )
    if alive_file.exists():
        first = alive_file.stat().st_mtime
        time.sleep(0.4)
        second = alive_file.stat().st_mtime
        assert second == first, (
            "grandchild alive-file continued to advance after dispatch — "
            "the worker subtree was not terminated"
        )


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process-group reaping requires POSIX os.killpg",
)
def test_active_dispatch_timeout_reaps_grandchildren(tmp_path):
    """Max-runtime timeout must kill the worker AND its grandchildren."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", _GRANDCHILD_PARENT_HB_TICKING],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-timeout-reap",
        max_runtime_seconds=0.5,
        heartbeat_stale_seconds=30.0,
        poll_interval=0.02,
    )

    assert result.status == "timeout"
    grandchild_pid = _read_grandchild_pid(Path(workspace.root))
    _assert_grandchild_reaped(Path(workspace.root), grandchild_pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process-group reaping requires POSIX os.killpg",
)
def test_active_dispatch_stale_heartbeat_reclaim_reaps_grandchildren(tmp_path):
    """Stale-heartbeat reclaim must kill the worker AND its grandchildren."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", _GRANDCHILD_PARENT_STALE_HB],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-stale-reap",
        max_runtime_seconds=30.0,
        heartbeat_stale_seconds=0.3,
        poll_interval=0.02,
    )

    assert result.status == "reclaimed"
    grandchild_pid = _read_grandchild_pid(Path(workspace.root))
    _assert_grandchild_reaped(Path(workspace.root), grandchild_pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process-group reaping requires POSIX os.killpg",
)
def test_active_dispatch_kill_switch_abort_reaps_grandchildren(tmp_path):
    """Mid-run kill-switch abort must kill the worker AND its grandchildren."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    ks = KillSwitch(tmp_path / "ks.json")

    pid_file = Path(workspace.root) / "grandchild.pid"

    def _arm_when_grandchild_ready() -> None:
        if pid_file.exists() and not ks.is_enabled():
            ks.enable(reason="phase10 abort reap test")

    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", _GRANDCHILD_PARENT_HB_TICKING],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-abort-reap",
        max_runtime_seconds=30.0,
        heartbeat_stale_seconds=30.0,
        poll_interval=0.02,
        kill_switch=ks,
        poll_hook=_arm_when_grandchild_ready,
    )

    assert result.status == "aborted"
    grandchild_pid = _read_grandchild_pid(Path(workspace.root))
    _assert_grandchild_reaped(Path(workspace.root), grandchild_pid)


# ---------------------------------------------------------------------------
# Opt-in WorkerResourceLimits
# ---------------------------------------------------------------------------


def test_worker_resource_limits_dataclass_defaults_are_all_none():
    """WorkerResourceLimits defaults are all None (= off) so existing callers
    see no behavior change."""
    from hermes_cli.team_os.loop_runner import WorkerResourceLimits

    limits = WorkerResourceLimits()
    assert limits.max_file_size_bytes is None
    assert limits.max_processes is None
    assert limits.max_address_space_bytes is None
    assert limits.max_cpu_seconds is None


def test_worker_resource_limits_explicit_values_stored():
    from hermes_cli.team_os.loop_runner import WorkerResourceLimits

    limits = WorkerResourceLimits(
        max_file_size_bytes=4096,
        max_processes=8,
        max_address_space_bytes=256 * 1024 * 1024,
        max_cpu_seconds=5,
    )
    assert limits.max_file_size_bytes == 4096
    assert limits.max_processes == 8
    assert limits.max_address_space_bytes == 256 * 1024 * 1024
    assert limits.max_cpu_seconds == 5


def test_run_active_dispatch_accepts_resource_limits_kwarg(tmp_path):
    """run_active_dispatch must accept resource_limits=None without error
    (back-compat for every existing caller)."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-rl-none",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        resource_limits=None,
    )
    assert result.status == "succeeded"


def test_resource_limits_default_off_does_not_alter_normal_dispatch(tmp_path):
    """Omitting the resource_limits kwarg entirely must work exactly as
    before — Phase 6/9/9A/9B back-compat guard."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-rl-omitted",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
    )
    assert result.status == "succeeded"


@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="rlimit application via preexec_fn requires POSIX fork",
)
def test_resource_limits_max_file_size_blocks_large_writes(tmp_path):
    """RLIMIT_FSIZE caps how much the worker can write to any single file.

    Reliable on macOS and Linux (POSIX-required behavior: SIGXFSZ on
    overflow).  We give a 4 KiB ceiling and ask the worker to write 256 KiB
    — the file must NOT grow past the cap.
    """
    from hermes_cli.team_os.loop_runner import (
        WorkerResourceLimits,
        run_active_dispatch,
    )

    workspace = _simple_workspace(tmp_path)
    out_path = Path(workspace.root) / "blob.bin"
    worker_script = textwrap.dedent(
        f"""
        import os, pathlib, time
        hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
        hb.write_text(str(time.time()))
        out = pathlib.Path({str(out_path)!r})
        try:
            with open(out, "wb") as f:
                f.write(b"x" * (256 * 1024))
        except OSError:
            pass
        """
    )

    run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", worker_script],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-rlimit-fsize",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        resource_limits=WorkerResourceLimits(max_file_size_bytes=4 * 1024),
    )

    # Load-bearing proof: the file must not exceed the rlimit cap.
    if out_path.exists():
        assert out_path.stat().st_size <= 4 * 1024, (
            f"RLIMIT_FSIZE leak: wrote {out_path.stat().st_size} bytes "
            f"with a 4 KiB cap"
        )


@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="rlimit application via preexec_fn requires POSIX fork",
)
@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="RLIMIT_NPROC is per-user on macOS and would limit the test host",
)
def test_resource_limits_max_processes_blocks_fork_bomb(tmp_path):
    """RLIMIT_NPROC caps how many processes the worker UID may have.

    Skipped on macOS where this limit is per-user and would constrain the
    entire test runner.  On Linux we prove the cap actually fires by
    forking until fork() raises BlockingIOError / OSError.
    """
    from hermes_cli.team_os.loop_runner import (
        WorkerResourceLimits,
        run_active_dispatch,
    )

    workspace = _simple_workspace(tmp_path)
    proof_path = Path(workspace.root) / "fork-fail.txt"
    worker_script = textwrap.dedent(
        f"""
        import os, pathlib, subprocess, sys, time
        hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
        hb.write_text(str(time.time()))
        proof = pathlib.Path({str(proof_path)!r})
        children = []
        try:
            for _ in range(64):
                children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"]))
        except (OSError, BlockingIOError) as exc:
            proof.write_text(repr(exc))
        finally:
            for c in children:
                try:
                    c.kill()
                except Exception:
                    pass
        """
    )

    run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", worker_script],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-rlimit-nproc",
        max_runtime_seconds=10.0,
        heartbeat_stale_seconds=10.0,
        poll_interval=0.05,
        resource_limits=WorkerResourceLimits(max_processes=4),
    )

    assert proof_path.exists(), "RLIMIT_NPROC never tripped — fork() did not fail"


@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="rlimit application via preexec_fn requires POSIX fork",
)
@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="RLIMIT_AS is unreliable on Darwin; modern allocators bypass it",
)
def test_resource_limits_max_address_space_blocks_huge_alloc(tmp_path):
    """RLIMIT_AS caps the worker virtual address space.

    Unreliable on macOS (modern allocators bypass it); only meaningful on
    Linux.  Set a tight 256 MiB cap and ask the worker to allocate ~1 GiB
    — must fail.
    """
    from hermes_cli.team_os.loop_runner import (
        WorkerResourceLimits,
        run_active_dispatch,
    )

    workspace = _simple_workspace(tmp_path)
    proof_path = Path(workspace.root) / "alloc-fail.txt"
    worker_script = textwrap.dedent(
        f"""
        import os, pathlib, time
        hb = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
        hb.write_text(str(time.time()))
        proof = pathlib.Path({str(proof_path)!r})
        try:
            b = bytearray(1024 * 1024 * 1024)  # 1 GiB
            del b
        except MemoryError as exc:
            proof.write_text(repr(exc))
        """
    )

    run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", worker_script],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-rlimit-as",
        max_runtime_seconds=10.0,
        heartbeat_stale_seconds=10.0,
        poll_interval=0.05,
        resource_limits=WorkerResourceLimits(max_address_space_bytes=256 * 1024 * 1024),
    )

    assert proof_path.exists(), "RLIMIT_AS never tripped — huge alloc succeeded"


# ---------------------------------------------------------------------------
# Fail-closed production audit guard
# ---------------------------------------------------------------------------


def test_run_active_dispatch_production_mode_without_audit_path_fails_closed(tmp_path):
    """``production_mode=True`` with ``audit_path=None`` must fail closed
    BEFORE acquiring the lock or spawning a worker.

    The CLI already supplies a default audit path; this branch protects
    every other caller (programmatic users, future tooling).
    """
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")  # disabled
    workspace = _simple_workspace(tmp_path)
    lock_path = tmp_path / "lock"

    result = run_active_dispatch(
        _make_task(
            approval_status="approved",
            task_confidence="high",
            quota_confidence="high",
        ),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=lock_path,
        owner="phase10-prod-no-audit",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        kill_switch=ks,
        production_mode=True,
        audit_path=None,
    )

    assert result.status == "production_audit_required"
    assert result.blocks_task is True
    assert result.production_mode is True
    assert "audit" in result.reason.lower()
    # Crucial fail-closed guarantee: no lock acquired, no worker spawned.
    assert not lock_path.exists(), (
        "fail-closed guard must run BEFORE lock acquisition"
    )


def test_run_active_dispatch_production_audit_required_serialises(tmp_path):
    """The new fail-closed status round-trips through DispatchResult.to_dict()."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-prod-serialise",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        kill_switch=ks,
        production_mode=True,
        audit_path=None,
    )

    data = result.to_dict()
    assert data["status"] == "production_audit_required"
    assert data["blocks_task"] is True
    assert data["production_mode"] is True
    json.dumps(data)  # must serialise


def test_run_active_dispatch_production_mode_with_audit_path_still_runs(tmp_path):
    """Sanity guard: providing audit_path lets production dispatch proceed
    (Phase 9 happy path is unchanged)."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    ks = KillSwitch(tmp_path / "ks.json")
    workspace = _simple_workspace(tmp_path)
    audit_path = tmp_path / "audit.jsonl"

    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-prod-ok",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        kill_switch=ks,
        production_mode=True,
        audit_path=audit_path,
    )

    assert result.status == "succeeded"
    assert audit_path.exists()


def test_sandbox_mode_without_audit_path_unaffected(tmp_path):
    """Default sandbox mode is completely unaffected by the production audit guard."""
    from hermes_cli.team_os.loop_runner import run_active_dispatch

    workspace = _simple_workspace(tmp_path)
    result = run_active_dispatch(
        _make_task(),
        workspace=workspace,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb",
        lock_path=tmp_path / "lock",
        owner="phase10-sandbox-no-audit",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.02,
        production_mode=False,
        audit_path=None,
    )
    assert result.status == "succeeded"
