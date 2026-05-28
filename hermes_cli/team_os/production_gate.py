"""Production-mode gate for Team OS — Phase 9.

All checks are pure data: no network, no subprocesses, no filesystem side-effects
(except write_production_audit which appends a JSONL audit trail).

Usage::

    from hermes_cli.team_os.production_gate import check_production_gate, write_production_audit

    result = check_production_gate(task, kill_switch=ks)
    if not result.passed:
        raise ProductionGateBlocked(result.violations)

    write_production_audit(task_id=task.task_id, ..., audit_path=audit_path)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import LoopTask

# Approval statuses that are acceptable for production execution.
_APPROVED_STATUSES = {"approved", "auto-approved"}

# Confidence levels that are acceptable for production execution.
_HIGH_CONFIDENCE = {"high", "medium"}

# Quota confidence levels that block production execution.
_BLOCKING_QUOTA = {"unknown", "low", "unavailable", "exhausted"}


class ProductionGateBlocked(RuntimeError):
    """Raised when the production gate check fails."""


@dataclass(frozen=True)
class ProductionGateResult:
    """Result of a production gate check.

    Attributes:
        passed: True if all checks passed; False if any violation was found.
        violations: Tuple of human-readable violation messages.
    """

    passed: bool
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": list(self.violations),
        }


def check_production_gate(
    task: "LoopTask",
    *,
    kill_switch: "KillSwitch | None",
) -> ProductionGateResult:
    """Run all production-mode checks against ``task``.

    Checks (in order, all accumulated):
      1. Kill-switch must be disabled.
      2. approval_status must be an explicitly-approved value.
      3. task_confidence must be assessed and high/medium (not None/low/unknown).
      4. quota_confidence must not be blocking.

    Args:
        task: The :class:`~hermes_cli.team_os.loop_runner.LoopTask` to check.
        kill_switch: The :class:`~hermes_cli.team_os.kill_switch.KillSwitch` instance.
            Must not be None for production execution.

    Returns:
        :class:`ProductionGateResult` with passed=True only when every check passes.
    """
    violations: list[str] = []

    # 1. Kill-switch check (required, not optional in production)
    if kill_switch is None:
        violations.append("kill-switch is required for production execution but was not provided")
    elif kill_switch.is_enabled():
        violations.append("kill-switch is enabled — production execution blocked")

    # 2. Approval check
    if task.approval_status not in _APPROVED_STATUSES:
        violations.append(
            f"approval_status={task.approval_status!r} is not an approved production value "
            f"(must be one of: {sorted(_APPROVED_STATUSES)})"
        )

    # 3. Task confidence check
    if task.task_confidence is None:
        violations.append(
            "task_confidence is None (not assessed) — production requires explicit confidence"
        )
    elif task.task_confidence not in _HIGH_CONFIDENCE:
        violations.append(
            f"task_confidence={task.task_confidence!r} is too low for production "
            f"(must be one of: {sorted(_HIGH_CONFIDENCE)})"
        )

    # 4. Quota confidence check
    if task.quota_confidence in _BLOCKING_QUOTA:
        violations.append(
            f"quota_confidence={task.quota_confidence!r} is not acceptable for production"
        )

    return ProductionGateResult(
        passed=len(violations) == 0,
        violations=tuple(violations),
    )


def write_production_audit(
    *,
    task_id: str,
    task_title: str,
    owner: str,
    approval_status: str | None,
    task_confidence: str | None,
    quota_confidence: str,
    workspace: str,
    audit_path: Path,
    decision: str = "allowed",
    violations: list[str] | None = None,
) -> None:
    """Append a production execution audit entry to ``audit_path`` (JSONL format).

    Creates parent directories and the file if they don't exist.
    Each call appends exactly one JSON line.

    Args:
        task_id: The task identifier.
        task_title: Human-readable task title.
        owner: The runner/owner identifier that authorized execution.
        approval_status: The approval status recorded on the task.
        task_confidence: The task confidence level.
        quota_confidence: The quota confidence level.
        workspace: Path of the sandbox workspace used for execution.
        audit_path: File path where the JSONL audit trail is written.
        decision: ``"allowed"`` (default, Phase 9 success path) or ``"denied"``
            (Phase 9B CLI gate rejection).
        violations: Gate violation strings; included only when non-empty.
    """
    audit_path = Path(audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    entry: dict[str, Any] = {
        "ts": _utc_iso(),
        "task_id": task_id,
        "task_title": task_title,
        "owner": owner,
        "approval_status": approval_status,
        "task_confidence": task_confidence,
        "quota_confidence": quota_confidence,
        "workspace": workspace,
        "decision": decision,
    }
    if violations:
        entry["violations"] = list(violations)

    line = json.dumps(entry, sort_keys=True) + "\n"
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _utc_iso() -> str:
    """Return current UTC time as a compact ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
