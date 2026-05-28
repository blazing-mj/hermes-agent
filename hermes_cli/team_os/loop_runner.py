"""Dry-run loop runner selection for Team OS Phase 4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_ELIGIBLE_APPROVAL_STATUSES = {None, "approved", "auto-approved"}
_BLOCKING_QUOTA_CONFIDENCE = {"unknown", "low", "unavailable", "exhausted"}


@dataclass(frozen=True)
class LoopTask:
    task_id: str
    title: str
    priority: int = 0
    status: str = "ready"
    shifts: tuple[str, ...] = ("day", "night")
    approval_status: str | None = None
    quota_confidence: str = "unknown"

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


def _skip_reason(task: LoopTask, *, current_shift: str) -> str | None:
    if task.status not in {"ready", "pending", "todo", "backlog"}:
        return f"status {task.status}"
    if current_shift not in task.shifts:
        return f"shift {current_shift} not allowed"
    if task.approval_status not in _ELIGIBLE_APPROVAL_STATUSES:
        return f"approval {task.approval_status}"
    if task.quota_confidence in _BLOCKING_QUOTA_CONFIDENCE:
        return f"quota confidence {task.quota_confidence}"
    return None


def select_next_task(tasks: Iterable[LoopTask], *, current_shift: str) -> LoopDecision:
    """Select the next eligible task without executing or mutating anything."""

    eligible: list[LoopTask] = []
    skipped: list[str] = []
    skip_reasons: dict[str, str] = {}
    for task in tasks:
        reason = _skip_reason(task, current_shift=current_shift)
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
