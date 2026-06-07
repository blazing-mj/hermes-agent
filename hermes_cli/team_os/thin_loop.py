"""Thin Team OS execution loop primitives.

The thin loop is intentionally small: one source ticket, one mission directory,
one isolated git worktree, one Developer slice, one Worker, one Validator gate.
It does not merge, live-dispatch, or mark Linear Done.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DENIED_SURFACES = (
    "prod",
    "production",
    "customer",
    "credential",
    "credentials",
    "secret",
    ".env",
    "gateway/runtime",
    "money",
    "billing",
)


@dataclass(frozen=True)
class MissionPaths:
    mission_dir: Path
    contract_path: Path
    lease_path: Path
    status_path: Path
    worktree_path: Path


def _safe_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "mission"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def build_thin_contract(
    *,
    source_ticket: str,
    title: str,
    allowed_files: list[str],
    required_commands: list[str],
) -> dict[str, Any]:
    """Build the narrow validation contract for one Team OS thin-loop proof."""

    return {
        "role": "planner-output",
        "source_ticket": source_ticket,
        "problem": title,
        "areas": ["Team OS planner-runner acceptance criteria"],
        "files_to_touch": allowed_files,
        "implementation_scope": [
            "Polish planner output acceptance criteria into concrete pass/fail conditions",
            "Reject generic acceptance criteria in the planner validator",
            "Keep changes within the declared file area",
        ],
        "acceptance_criteria": [
            "Planner output acceptance_criteria contain concrete pass/fail conditions tied to observable behavior",
            "Planner validator returns BOUNCE when acceptance_criteria contain generic phrasing such as complete the subtask without observable details",
            "Focused tests prove generic criteria bounce and crisp criteria pass",
        ],
        "proof_required": [
            "Focused pytest output for planner-runner acceptance criteria tests",
            "git diff lines quoted by Validator for every accepted worker claim",
        ],
        "required_commands": required_commands,
        "commands": required_commands,
        "intended_behavior": "AGENTS-172: Planner acceptance criteria are crisp, testable, and Validator-bounced when generic.",
        "non_goals": [
            "Do not merge",
            "Do not live-dispatch",
            "Do not mark Linear Done",
            "Do not change runtime provider configuration",
        ],
        "assertions": [
            "Generic acceptance criteria are detected and bounced",
            "Generated planner contracts use crisp observable acceptance criteria",
            "Only declared planner files/tests are changed",
            "Human gate remains required and auto-Done remains disabled",
        ],
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": [
            "Worker handoff lacks focused proof output",
            "Worker claims are not backed by quoted git diff lines",
            "Changed files escape the allowed file area",
            "Generic acceptance criteria still pass validation",
            "Any merge/live-dispatch/auto-Done attempt appears",
        ],
    }


def check_thin_loop_boundaries(contract: dict[str, Any]) -> list[str]:
    """Return deny reasons before any worktree or agent execution."""

    violations: list[str] = []
    for field in ("files_to_touch", "areas", "implementation_scope", "non_goals"):
        value = contract.get(field, [])
        items = value if isinstance(value, list) else [str(value)]
        for item in items:
            lowered = str(item).lower()
            for denied in _DENIED_SURFACES:
                if denied in lowered:
                    violations.append(f"denied surface in {field}: {item}")
                    break
    if contract.get("human_gate_required") is not True:
        violations.append("human_gate_required must stay true")
    return violations


def prepare_thin_loop_mission(
    *,
    repo_root: Path,
    state_root: Path,
    worktree_root: Path,
    contract: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create mission artifacts and one isolated git worktree, fail-closed."""

    source_ticket = str(contract.get("source_ticket") or "unknown")
    run_id = run_id or str(int(time.time()))
    mission_dir = state_root.expanduser() / source_ticket / run_id
    worktree_path = worktree_root.expanduser() / f"{_safe_slug(source_ticket)}-{_safe_slug(run_id)}"
    paths = MissionPaths(
        mission_dir=mission_dir,
        contract_path=mission_dir / "contract.json",
        lease_path=mission_dir / "lease.json",
        status_path=mission_dir / "status.json",
        worktree_path=worktree_path,
    )

    boundary_violations = check_thin_loop_boundaries(contract)
    if boundary_violations:
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "denied",
                "violations": boundary_violations,
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "denied", "violations": boundary_violations, "paths": _paths_dict(paths)}

    if paths.lease_path.exists():
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "lease_denied",
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "lease_denied", "paths": _paths_dict(paths)}

    _write_json(paths.contract_path, contract)
    _write_json(
        paths.lease_path,
        {
            "owner": "team-os-thin-loop",
            "pid": os.getpid(),
            "source_ticket": source_ticket,
            "run_id": run_id,
            "acquired_at": time.time(),
            "worktree_path": str(worktree_path),
        },
    )
    _write_json(
        paths.status_path,
        {
            "source_ticket": source_ticket,
            "run_id": run_id,
            "status": "prepared",
            "worktree_created": False,
            "auto_done_allowed": False,
            "live_dispatch_allowed": False,
        },
    )

    worktree_root.expanduser().mkdir(parents=True, exist_ok=True)
    branch = f"team-os/{_safe_slug(source_ticket)}-{_safe_slug(run_id)}"
    result = _run_git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo_root.expanduser())
    if result.returncode != 0:
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "worktree_failed",
                "error": result.stderr or result.stdout,
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "worktree_failed", "error": result.stderr or result.stdout, "paths": _paths_dict(paths)}

    _write_json(
        paths.status_path,
        {
            "source_ticket": source_ticket,
            "run_id": run_id,
            "status": "prepared",
            "worktree_created": True,
            "worktree_path": str(worktree_path),
            "branch": branch,
            "auto_done_allowed": False,
            "live_dispatch_allowed": False,
        },
    )
    return {"ok": True, "status": "prepared", "branch": branch, "paths": _paths_dict(paths)}


def _paths_dict(paths: MissionPaths) -> dict[str, str]:
    return {
        "mission_dir": str(paths.mission_dir),
        "contract_path": str(paths.contract_path),
        "lease_path": str(paths.lease_path),
        "status_path": str(paths.status_path),
        "worktree_path": str(paths.worktree_path),
    }
