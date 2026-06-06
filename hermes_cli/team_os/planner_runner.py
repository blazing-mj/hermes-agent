"""Dry-run Planner-runner for Team OS goal contracts.

The planner-runner is deliberately deterministic and offline. It turns one
source goal into reviewable subtasks and validation contracts, then runs a
cold-style intent-preservation validator before anything can feed the loop.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .contracts import check_contract
from .decomposer import decompose_goal

_STOPWORDS = frozenset(
    {
        "with",
        "that",
        "this",
        "from",
        "into",
        "before",
        "after",
        "keep",
        "human",
        "goal",
        "linear",
        "step",
        "task",
        "tasks",
        "real",
        "only",
        "must",
        "does",
        "done",
        "auto",
        "gate",
        "review",
        "wire",
        "team",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]{3,}", re.IGNORECASE)


def _intent_tokens(*parts: str) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        for token in _TOKEN_RE.findall(part.lower()):
            normalized = token.strip("-_")
            if normalized and normalized not in _STOPWORDS:
                tokens.add(normalized)
    return tokens


def _build_validation_contract(
    *,
    goal_id: str,
    goal_title: str,
    task: Any,
) -> dict[str, Any]:
    title = goal_title or goal_id
    return {
        "role": "planner-output",
        "source_ticket": goal_id,
        "intended_behavior": (
            f"Preserve source goal intent for {goal_id} ({title}) while completing "
            f"planner subtask {task.task_id}: {task.description}"
        ),
        "non_goals": [
            "Do not feed this Planner output into the loop before human review",
            "Do not auto-dispatch Developer/Worker execution from this output",
            "Do not auto-Done the Linear issue from this output",
            "Do not expand scope beyond the source goal intent",
        ],
        "assertions": [
            f"Intent preserved from source goal {goal_id}: {title}",
            f"Subtask remains tied to planner description: {task.description}",
            "Validator must check source-goal intent preservation, not only schema validity",
            "Human approval is recorded before any loop feed or downstream Worker execution",
        ],
        "commands": list(task.verifier_plan),
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": [
            "Planner contract no longer preserves the source goal intent",
            "Validation contract schema fails",
            "Human gate is not required",
            "Output attempts to auto-dispatch, feed the loop, or auto-Done",
            "Subtask confidence is low or unknown for a runnable Worker task",
        ],
    }


def validate_planner_output(
    *,
    goal_id: str,
    goal_title: str,
    goal_body: str,
    planned_tasks: Sequence[dict[str, Any]],
    loop_feed_allowed: bool,
    auto_dispatch_allowed: bool = False,
    auto_done_allowed: bool = False,
) -> dict[str, Any]:
    """Validate Planner output before loop feed.

    This is intentionally more than a schema check: it verifies that each
    subtask+contract still carries source-goal intent. A valid-looking contract
    about the wrong work must BOUNCE.
    """
    errors: list[str] = []
    schema_valid = True
    intent_preserved = True

    if loop_feed_allowed:
        errors.append("loop feed must remain disabled until human review")
    if auto_dispatch_allowed:
        errors.append("auto-dispatch must remain disabled until human review")
    if auto_done_allowed:
        errors.append("auto-Done must remain disabled until human review")

    if not goal_title.strip():
        errors.append("intent check failed: source goal title is empty")
        intent_preserved = False

    if not planned_tasks:
        errors.append("intent check failed: planner produced no tasks")
        intent_preserved = False

    goal_tokens = _intent_tokens(goal_title, goal_body)
    title_tokens = _intent_tokens(goal_title)
    if not goal_tokens:
        errors.append("intent check failed: source goal has no meaningful intent tokens")
        intent_preserved = False

    for idx, planned in enumerate(planned_tasks):
        prefix = f"task[{idx}]"
        contract = planned.get("validation_contract")
        if not isinstance(contract, dict):
            errors.append(f"{prefix}: missing validation_contract")
            schema_valid = False
            intent_preserved = False
            continue

        contract_errors = check_contract(contract)
        if contract_errors:
            schema_valid = False
            errors.extend(f"{prefix}: contract {err}" for err in contract_errors)

        if contract.get("source_ticket") != goal_id:
            errors.append(f"{prefix}: contract source_ticket does not match {goal_id}")
            intent_preserved = False

        if contract.get("human_gate_required") is not True:
            errors.append(f"{prefix}: human_gate_required must stay true")

        task_text = " ".join(
            [
                str(planned.get("title", "")),
                str(planned.get("description", "")),
            ]
        )
        task_tokens = _intent_tokens(task_text)
        if title_tokens and not (title_tokens & task_tokens):
            errors.append(
                f"{prefix}: task intent check failed — no source-title tokens preserved in task title/description"
            )
            intent_preserved = False
        if goal_tokens and len(goal_tokens & task_tokens) < min(2, len(goal_tokens)):
            errors.append(
                f"{prefix}: task intent check failed — insufficient source-goal token overlap in task title/description"
            )
            intent_preserved = False

        contract_corpus = " ".join(
            [
                str(contract.get("intended_behavior", "")),
                " ".join(contract.get("assertions", []) if isinstance(contract.get("assertions"), list) else []),
            ]
        )
        contract_tokens = _intent_tokens(contract_corpus)
        if goal_tokens and len(goal_tokens & contract_tokens) < min(2, len(goal_tokens)):
            errors.append(
                f"{prefix}: intent check failed — insufficient source-goal token overlap in validation contract"
            )
            intent_preserved = False

    verdict = "PASS" if not errors and schema_valid and intent_preserved else "BOUNCE"
    return {
        "verdict": verdict,
        "schema_valid": schema_valid,
        "intent_preserved": intent_preserved,
        "loop_feed_allowed": loop_feed_allowed,
        "human_review_required": True,
        "errors": errors,
        "checks": [
            "contract schema valid",
            "source_ticket matches source goal",
            "source-title/source-goal tokens preserved in each task contract",
            "human gate required",
            "loop feed disabled before human review",
        ],
    }


def plan_goal(
    *,
    goal_id: str,
    goal_title: str,
    goal_body: str,
    labels: Sequence[str],
    max_tasks: int = 10,
) -> dict[str, Any]:
    """Plan one goal into reviewable subtasks + contracts without dispatching."""
    tasks = decompose_goal(
        goal_id=goal_id,
        goal_title=goal_title,
        goal_body=goal_body,
        labels=labels,
        max_tasks=max_tasks,
    )
    planned_tasks: list[dict[str, Any]] = []
    for task in tasks:
        task_data = task.to_dict()
        task_data["validation_contract"] = _build_validation_contract(
            goal_id=goal_id,
            goal_title=goal_title,
            task=task,
        )
        planned_tasks.append(task_data)

    loop_feed_allowed = False
    review = validate_planner_output(
        goal_id=goal_id,
        goal_title=goal_title,
        goal_body=goal_body,
        planned_tasks=planned_tasks,
        loop_feed_allowed=loop_feed_allowed,
        auto_dispatch_allowed=False,
        auto_done_allowed=False,
    )
    return {
        "schema": "team_os.planner_run.v1",
        "goal_id": goal_id,
        "goal_title": goal_title,
        "goal_body": goal_body,
        "labels": list(labels),
        "dry_run": True,
        "loop_feed_allowed": loop_feed_allowed,
        "human_review_required": True,
        "auto_dispatch_allowed": False,
        "auto_done_allowed": False,
        "tasks": planned_tasks,
        "planner_review": review,
    }
