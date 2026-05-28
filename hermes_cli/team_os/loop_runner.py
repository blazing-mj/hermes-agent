"""Loop runner for Team OS — Phase 4 dry-run selection + Phase 6 active dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

_ELIGIBLE_APPROVAL_STATUSES = {None, "approved", "auto-approved"}
_BLOCKING_QUOTA_CONFIDENCE = {"unknown", "low", "unavailable", "exhausted"}
_BLOCKING_TASK_CONFIDENCE = {"unknown", "low"}


@dataclass(frozen=True)
class LoopTask:
    task_id: str
    title: str
    priority: int = 0
    status: str = "ready"
    shifts: tuple[str, ...] = ("day", "night")
    approval_status: str | None = None
    quota_confidence: str = "unknown"
    task_confidence: str | None = None  # Phase 8: None means not yet assessed (pass-through)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoopTask:
        shifts_raw = data.get("shifts") or ("day", "night")
        return cls(
            task_id=str(data["task_id"]),
            title=str(data.get("title") or data["task_id"]),
            priority=int(data.get("priority", 0)),
            status=str(data.get("status", "ready")),
            shifts=tuple(str(shift) for shift in shifts_raw),
            approval_status=data.get("approval_status"),
            quota_confidence=str(data.get("quota_confidence", "unknown")),
            task_confidence=data.get("task_confidence"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "priority": self.priority,
            "status": self.status,
            "shifts": list(self.shifts),
            "approval_status": self.approval_status,
            "quota_confidence": self.quota_confidence,
            "task_confidence": self.task_confidence,
        }


@dataclass(frozen=True)
class LoopDecision:
    selected_task_id: str | None
    selected_task: LoopTask | None
    skipped_task_ids: tuple[str, ...]
    skip_reasons: dict[str, str]
    dry_run: bool = True
    would_spawn_worker: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_task_id": self.selected_task_id,
            "selected_task": self.selected_task.to_dict() if self.selected_task else None,
            "skipped_task_ids": list(self.skipped_task_ids),
            "skip_reasons": dict(self.skip_reasons),
            "dry_run": self.dry_run,
            "would_spawn_worker": self.would_spawn_worker,
        }


class RunnerAlreadyActive(RuntimeError):
    """Raised when a loop-runner lock already exists."""


@dataclass(frozen=True)
class RunnerLock:
    path: Path
    owner: str

    def exists(self) -> bool:
        return self.path.exists()

    def release(self) -> None:
        if self.path.exists() and self.path.read_text(encoding="utf-8") == self.owner:
            self.path.unlink()


def _skip_reason(task: LoopTask, *, current_shift: str, require_confidence: bool = False) -> str | None:
    if task.status not in {"ready", "pending", "todo", "backlog"}:
        return f"status {task.status}"
    if current_shift not in task.shifts:
        return f"shift {current_shift} not allowed"
    if task.approval_status not in _ELIGIBLE_APPROVAL_STATUSES:
        return f"approval {task.approval_status}"
    if task.quota_confidence in _BLOCKING_QUOTA_CONFIDENCE:
        return f"quota confidence {task.quota_confidence}"
    # Phase 8: task_confidence=None means "not assessed by decomposer" — backward-compatible
    # pass-through. Only block on low/unknown when explicitly set.
    if task.task_confidence is not None and task.task_confidence in _BLOCKING_TASK_CONFIDENCE:
        return f"task confidence {task.task_confidence}"
    if require_confidence and task.task_confidence is None:
        return "task confidence not assessed"
    return None


def select_next_task(
    tasks: Iterable[LoopTask],
    *,
    current_shift: str,
    require_confidence: bool = False,
) -> LoopDecision:
    """Select the next eligible task without executing or mutating anything.

    Args:
        require_confidence: When True, tasks with task_confidence=None are also
            blocked (skip reason "task confidence not assessed"). Default False
            preserves backward compatibility where None is a pass-through.
    """

    eligible: list[LoopTask] = []
    skipped: list[str] = []
    skip_reasons: dict[str, str] = {}
    for task in tasks:
        reason = _skip_reason(
            task,
            current_shift=current_shift,
            require_confidence=require_confidence,
        )
        if reason:
            skipped.append(task.task_id)
            skip_reasons[task.task_id] = reason
        else:
            eligible.append(task)

    eligible.sort(key=lambda task: (-task.priority, task.task_id))
    selected = eligible[0] if eligible else None
    return LoopDecision(
        selected_task_id=selected.task_id if selected else None,
        selected_task=selected,
        skipped_task_ids=tuple(skipped),
        skip_reasons=skip_reasons,
        dry_run=True,
        would_spawn_worker=False,
    )

def load_loop_tasks(path: Path) -> list[LoopTask]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("loop task fixture must be a JSON list")
    return [LoopTask.from_dict(item) for item in data]


def write_loop_decision(decision: LoopDecision, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(decision.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def acquire_runner_lock(lock_path: Path, *, owner: str) -> RunnerLock:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        existing_owner = lock_path.read_text(encoding="utf-8") if lock_path.exists() else "unknown"
        raise RunnerAlreadyActive(f"loop runner already active: {existing_owner}") from exc
    with fd:
        fd.write(owner)
    return RunnerLock(path=lock_path, owner=owner)


# ---------------------------------------------------------------------------
# Phase 6 — Active dispatch against a sandbox workspace.
#
# Boundaries enforced here, by design:
#   * Only one worker runs at a time (RunnerLock).
#   * The worker only ever sees a SandboxWorkspace root that lives under an
#     explicitly-allowed prefix AND is not inside the Hermes repo itself.
#   * The runner does not itself use the network or spawn real agents; it
#     executes only the explicit local argv supplied by the caller.
#   * The runner enforces a max runtime and a heartbeat-staleness reclaim
#     window so a hung or crashed worker cannot silently hold the slot.
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = {"succeeded", "failed", "reclaimed", "timeout"}


class SandboxBoundaryViolation(RuntimeError):
    """Raised when an active dispatch target escapes the sandbox."""


def _hermes_repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / ".git").exists() or (parent / "flake.nix").exists():
            return parent
    return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class SandboxWorkspace:
    root: Path
    allowed_prefix: Path

    @classmethod
    def create(cls, root: Path, *, allowed_prefix: Path) -> "SandboxWorkspace":
        resolved_root = Path(root).expanduser().resolve()
        resolved_prefix = Path(allowed_prefix).expanduser().resolve()

        if not resolved_root.exists():
            raise SandboxBoundaryViolation(
                f"sandbox workspace does not exist: {resolved_root}"
            )
        if not resolved_root.is_dir():
            raise SandboxBoundaryViolation(
                f"sandbox workspace must be a directory: {resolved_root}"
            )
        if not _is_within(resolved_root, resolved_prefix):
            raise SandboxBoundaryViolation(
                f"workspace {resolved_root} is outside allowed sandbox prefix {resolved_prefix}"
            )

        repo_root = _hermes_repo_root()
        if repo_root is not None and _is_within(resolved_root, repo_root):
            raise SandboxBoundaryViolation(
                f"workspace {resolved_root} points inside the hermes repo {repo_root}; "
                "active dispatch refuses to operate on production paths"
            )

        return cls(root=resolved_root, allowed_prefix=resolved_prefix)

    def to_dict(self) -> dict[str, Any]:
        return {"root": str(self.root), "allowed_prefix": str(self.allowed_prefix)}


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    status: str
    exit_code: int | None
    reason: str
    workspace: str
    heartbeats_observed: int
    started_at: float
    ended_at: float
    blocks_task: bool
    owner: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "workspace": self.workspace,
            "heartbeats_observed": self.heartbeats_observed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blocks_task": self.blocks_task,
            "owner": self.owner,
            "dry_run": self.dry_run,
        }


def _terminate_process(proc: "subprocess.Popen[Any]", *, grace: float = 0.25) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.02)
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _heartbeat_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _sandbox_worker_env(task: LoopTask, *, heartbeat_path: Path, workspace: SandboxWorkspace) -> dict[str, str]:
    allowed_parent_keys = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH")
    env = {key: value for key, value in os.environ.items() if key in allowed_parent_keys}
    env["HERMES_HEARTBEAT_PATH"] = str(heartbeat_path)
    env["HERMES_SANDBOX_WORKSPACE"] = str(workspace.root)
    env["HERMES_TASK_ID"] = task.task_id
    return env


def run_active_dispatch(
    task: LoopTask,
    *,
    workspace: SandboxWorkspace,
    worker_command: Sequence[str],
    heartbeat_path: Path,
    lock_path: Path,
    owner: str,
    max_runtime_seconds: float,
    heartbeat_stale_seconds: float,
    poll_interval: float = 0.05,
) -> DispatchResult:
    """Run a single sandbox worker for ``task`` while honouring the lock,
    heartbeat-staleness reclaim window, and max runtime.

    The worker is spawned with ``cwd`` set to the sandbox workspace root and
    two env vars: ``HERMES_HEARTBEAT_PATH`` and ``HERMES_SANDBOX_WORKSPACE``.
    The runner never reaches the network and never touches paths outside the
    sandbox workspace.
    """

    if not worker_command:
        raise ValueError("worker_command must be a non-empty argv sequence")

    heartbeat_path = Path(heartbeat_path)
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    if heartbeat_path.exists():
        try:
            heartbeat_path.unlink()
        except FileNotFoundError:
            pass

    lock = acquire_runner_lock(Path(lock_path), owner=owner)

    started_at = time.time()
    status: str | None = None
    reason = ""
    exit_code: int | None = None
    heartbeats_observed = 0
    last_seen_mtime: float | None = None

    try:
        env = _sandbox_worker_env(task, heartbeat_path=heartbeat_path, workspace=workspace)

        proc = subprocess.Popen(  # noqa: S603 — caller-supplied argv, sandboxed cwd
            list(worker_command),
            cwd=str(workspace.root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            while True:
                now = time.time()
                elapsed = now - started_at

                current_mtime = _heartbeat_mtime(heartbeat_path)
                if current_mtime is not None and (
                    last_seen_mtime is None or current_mtime > last_seen_mtime
                ):
                    heartbeats_observed += 1
                    last_seen_mtime = current_mtime

                rc = proc.poll()
                if rc is not None:
                    exit_code = rc
                    if rc == 0:
                        status = "succeeded"
                        reason = "worker exited cleanly (exit 0)"
                    else:
                        status = "failed"
                        reason = f"worker exited with non-zero exit code {rc}"
                    break

                if elapsed > max_runtime_seconds:
                    status = "timeout"
                    reason = (
                        f"max runtime {max_runtime_seconds:.2f}s exceeded; "
                        "worker terminated"
                    )
                    break

                # Heartbeat staleness check.
                if last_seen_mtime is None:
                    # No heartbeat written yet — start-up grace == stale window.
                    if elapsed > heartbeat_stale_seconds:
                        status = "reclaimed"
                        reason = (
                            f"no heartbeat written within "
                            f"{heartbeat_stale_seconds:.2f}s startup window"
                        )
                        break
                else:
                    age = now - last_seen_mtime
                    if age > heartbeat_stale_seconds:
                        status = "reclaimed"
                        reason = (
                            f"heartbeat stale for {age:.2f}s "
                            f"(>{heartbeat_stale_seconds:.2f}s); worker reclaimed"
                        )
                        break

                time.sleep(poll_interval)
        finally:
            if status in {"timeout", "reclaimed"} or (status is None and proc.poll() is None):
                _terminate_process(proc)
                if exit_code is None:
                    exit_code = proc.poll()

        if status is None:  # defensive — shouldn't happen, but never lie about state.
            status = "reclaimed"
            reason = reason or "dispatch loop exited without terminal status"
    finally:
        lock.release()

    ended_at = time.time()
    blocks_task = status != "succeeded"
    return DispatchResult(
        task_id=task.task_id,
        status=status,
        exit_code=exit_code,
        reason=reason,
        workspace=str(workspace.root),
        heartbeats_observed=heartbeats_observed,
        started_at=started_at,
        ended_at=ended_at,
        blocks_task=blocks_task,
        owner=owner,
    )


def write_dispatch_result(result: DispatchResult, output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path
