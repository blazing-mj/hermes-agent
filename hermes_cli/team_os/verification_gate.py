"""Verification gate primitives for Team OS Phase 3."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

_RUNTIME_SMOKE_FILES = (
    "tests/test_current_work.py",
    "tests/gateway/test_unknown_command.py",
)
_RUNTIME_PATH_PREFIXES = (
    "agent/",
    "gateway/",
    "hermes_cli/main.py",
)
_RUNTIME_PATH_PARTS = (
    "config",
    "compression",
    "current_work",
)


class VerificationStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "argv": list(self.argv)}


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    exit_code: int
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "output": self.output,
        }


@dataclass(frozen=True)
class VerificationPlan:
    task_id: str
    commands: tuple[VerificationCommand, ...]
    requires_full_smoke: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "requires_full_smoke": self.requires_full_smoke,
            "commands": [command.to_dict() for command in self.commands],
        }


@dataclass(frozen=True)
class VerificationReport:
    task_id: str
    status: VerificationStatus
    can_close: bool
    commands: tuple[CommandResult, ...]
    proof_artifact: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "can_close": self.can_close,
            "proof_artifact": self.proof_artifact,
            "commands": [command.to_dict() for command in self.commands],
        }


def _runtime_smoke_required(changed_files: Iterable[str]) -> bool:
    for changed_file in changed_files:
        normalized = changed_file.replace("\\", "/")
        if normalized.startswith(_RUNTIME_PATH_PREFIXES):
            return True
        if any(part in normalized for part in _RUNTIME_PATH_PARTS):
            return True
    return False


def build_verification_plan(
    *,
    task_id: str,
    changed_files: Iterable[str],
    focused_tests: Iterable[str],
) -> VerificationPlan:
    """Select the smallest useful verification command set for a Hermes task."""

    py_files = tuple(path for path in changed_files if path.endswith(".py"))
    commands: list[VerificationCommand] = []
    if py_files:
        commands.append(VerificationCommand("syntax", (sys.executable, "-m", "py_compile", *py_files)))
        commands.append(VerificationCommand("lint", ("ruff", "check", *py_files)))

    tests = tuple(focused_tests)
    if tests:
        commands.append(VerificationCommand("focused-tests", (sys.executable, "-m", "pytest", "-o", "addopts=", *tests)))

    requires_full_smoke = _runtime_smoke_required(changed_files)
    if requires_full_smoke:
        commands.append(
            VerificationCommand(
                "runtime-smoke",
                (sys.executable, "-m", "pytest", "-o", "addopts=", *_RUNTIME_SMOKE_FILES),
            )
        )

    return VerificationPlan(task_id=task_id, commands=tuple(commands), requires_full_smoke=requires_full_smoke)


def run_verification_plan(plan: VerificationPlan, *, cwd: Path | None = None) -> VerificationReport:
    results: list[CommandResult] = []
    for command in plan.commands:
        completed = subprocess.run(  # noqa: S603
            command.argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        results.append(
            CommandResult(
                name=command.name,
                argv=command.argv,
                exit_code=completed.returncode,
                output=output[-12000:],
            )
        )

    passed = all(result.exit_code == 0 for result in results) and bool(results)
    status = VerificationStatus.PASSED if passed else VerificationStatus.FAILED
    return VerificationReport(
        task_id=plan.task_id,
        status=status,
        can_close=passed,
        commands=tuple(results),
        proof_artifact=None,
    )


def write_proof_artifact(report: VerificationReport, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_with_artifact = VerificationReport(
        task_id=report.task_id,
        status=report.status,
        can_close=report.can_close,
        commands=report.commands,
        proof_artifact=str(output_path),
    )
    output_path.write_text(json.dumps(report_with_artifact.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path
