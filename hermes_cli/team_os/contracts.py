"""Validation contract primitives for Team OS Phase 11."""

from __future__ import annotations

import copy
from typing import Any

from hermes_cli.team_os.role_registry import validator_contract_route

VALID_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "critical"})

_REQUIRED_STRING_FIELDS = ("intended_behavior", "source_ticket")
_REQUIRED_LIST_FIELDS = ("non_goals", "assertions", "commands", "bounce_conditions")
_REQUIRED_BOOL_FIELDS = ("behavior_check_required", "human_gate_required")


def check_contract(data: dict[str, Any]) -> list[str]:
    """Validate a contract dict; return a list of error strings (empty = valid)."""
    errors: list[str] = []

    for name in _REQUIRED_STRING_FIELDS:
        if name not in data:
            errors.append(f"missing required field: {name!r}")
        elif not isinstance(data[name], str):
            errors.append(f"{name!r} must be a string")
        elif not data[name].strip():
            errors.append(f"{name!r} must not be empty")

    for name in _REQUIRED_LIST_FIELDS:
        if name not in data:
            errors.append(f"missing required field: {name!r}")
        elif not isinstance(data[name], list):
            errors.append(f"{name!r} must be a list")
        elif not data[name]:
            errors.append(f"{name!r} must not be empty")
        else:
            for i, item in enumerate(data[name]):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"{name!r}[{i}] must be a non-empty string")

    for name in _REQUIRED_BOOL_FIELDS:
        if name not in data:
            errors.append(f"missing required field: {name!r}")
        elif not isinstance(data[name], bool):
            errors.append(f"{name!r} must be a bool")

    if "risk" not in data:
        errors.append("missing required field: 'risk'")
    elif not isinstance(data["risk"], str):
        errors.append("'risk' must be a string")
    elif data["risk"] not in VALID_RISK_LEVELS:
        errors.append(f"'risk' must be one of {sorted(VALID_RISK_LEVELS)}")

    return errors


# --- Deterministic handoff templates ---

PLANNER_TEMPLATE: dict[str, Any] = {
    "role": "planner",
    "intended_behavior": (
        "Decompose the goal into concrete subtasks with confidence scoring "
        "and prerequisite ordering"
    ),
    "non_goals": [
        "Do not execute any subtasks directly",
        "Do not modify production state",
        "Do not access external networks",
    ],
    "assertions": [
        "All subtasks reference a source_ticket",
        "Confidence is one of: high, medium, low",
        "Prerequisites are listed for each subtask",
    ],
    "commands": [
        "hermes team-os decompose-goal <goal_id> --goal-title '<title>'",
    ],
    "behavior_check_required": True,
    "risk": "low",
    "human_gate_required": False,
    "bounce_conditions": [
        "Goal body is empty or ambiguous",
        "Fewer than two subtasks produced",
        "Any subtask confidence is 'unknown'",
    ],
    "source_ticket": "AGENTS-136",
}

WORKER_TEMPLATE: dict[str, Any] = {
    "role": "worker",
    "intended_behavior": (
        "Execute a focused implementation task inside a sandboxed worktree "
        "and write a proof artifact"
    ),
    "definition_of_done": (
        "The task's focused tests run and pass (exit 0) and the proof is "
        "captured in proof_results — a concrete, runnable success check, not a "
        "prose claim"
    ),
    "non_goals": [
        "Do not expand scope beyond the assigned task",
        "Do not touch files unrelated to the task",
        "Do not push to the remote without human approval",
    ],
    "assertions": [
        "All focused tests pass with exit code 0",
        "Lint is clean (ruff check exits 0)",
        "Proof artifact is written to the output path",
    ],
    "commands": [
        "hermes team-os loop-runner --active --tasks <tasks.json>",
        "hermes team-os verification-gate <task_id> --changed-file <file>",
    ],
    "behavior_check_required": True,
    "risk": "medium",
    "human_gate_required": False,
    "bounce_conditions": [
        "Any focused test fails",
        "Lint reports errors",
        "Kill-switch is active",
        "Proof artifact is absent",
    ],
    "source_ticket": "AGENTS-136",
}

VALIDATOR_TEMPLATE: dict[str, Any] = {
    "role": "validator",
    "intended_behavior": (
        "Validate worker output against contract assertions and human-gate "
        "the handoff before task closure"
    ),
    "non_goals": [
        "Do not re-implement the task",
        "Do not approve without a proof artifact",
        "Do not close the ticket without human sign-off",
    ],
    "assertions": [
        "Proof artifact exists and is parseable JSON",
        "All contract assertions are verified",
        "No bounce conditions are triggered",
    ],
    "commands": [
        "hermes team-os verification-gate <task_id> --plan-only",
        "run_adversarial_validator --runner claude-max-code --contract <contract.json> --handoff <handoff.json>",
    ],
    "validator_route": validator_contract_route(),
    "behavior_check_required": True,
    "risk": "low",
    "human_gate_required": True,
    "bounce_conditions": [
        "Proof artifact is missing",
        "Any assertion is unverified",
        "Open questions remain in the task body",
    ],
    "source_ticket": "AGENTS-136",
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "planner": PLANNER_TEMPLATE,
    "worker": WORKER_TEMPLATE,
    "validator": VALIDATOR_TEMPLATE,
}


def render_template(role: str | None) -> dict[str, Any]:
    """Return a copy of the deterministic template for *role*.

    Raises ValueError for unknown or missing roles.
    """
    if role not in TEMPLATES:
        raise ValueError(f"unknown role {role!r}; choose one of {sorted(TEMPLATES)}")
    return copy.deepcopy(TEMPLATES[role])
