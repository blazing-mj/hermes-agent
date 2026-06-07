"""Dry-run Planner-runner for Team OS goal contracts.

The planner-runner is deliberately deterministic and offline. It turns one
source goal into reviewable subtasks and validation contracts, then runs a
cold-style intent-preservation validator before anything can feed the loop.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .contracts import check_contract
from .decomposer import CandidateTask, decompose_goal

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


def _is_status_note(description: str) -> bool:
    text = description.lower()
    status_markers = (
        "status hygiene",
        "is done",
        "already done",
        "remaining pending",
        "drop it from pending",
        "pat rotation",
        "bitwarden",
    )
    worker_markers = (
        "add ",
        "implement",
        "wire",
        "fix",
        "test",
        "prove",
        "update ",
        "locate",
        "regression",
    )
    return any(marker in text for marker in status_markers) and not any(
        marker in text for marker in worker_markers
    )


def _infer_files_and_areas(goal_title: str, description: str) -> tuple[list[str], list[str]]:
    text = f"{goal_title} {description}".lower()
    files: list[str] = []
    areas: list[str] = []
    if "watchdog" in text or "gateway_state" in text or "active_agents" in text or "stuck-busy" in text:
        areas.extend(["Hermes gateway watchdog", "gateway runtime status heartbeat"])
        files.extend([
            "hermes_cli/gateway_watchdog.py",
            "scripts/hermes-gateway-watchdog",
            "gateway/status.py",
            "tests/hermes_cli/test_gateway_watchdog.py",
        ])
    elif "telegram" in text or "gateway" in text or "media" in text or "attachment" in text:
        areas.extend(["gateway media attachment handling", "Telegram delivery path"])
        files.extend([
            "gateway/",
            "gateway/platforms/telegram*",
            "tests/gateway/ or tests/hermes_cli/ focused regression",
        ])
    if "team os" in text or "planner" in text or "contract" in text:
        areas.extend(["Team OS planner-runner", "validation contract generation"])
        files.extend([
            "hermes_cli/team_os/planner_runner.py",
            "hermes_cli/team_os/cli.py",
            "tests/hermes_cli/test_team_os_planner_runner.py",
        ])
    if "test" in text or "regression" in text or "pytest" in text:
        areas.append("focused automated tests")
        files.append("tests/")
    if "user-facing" in text or "message" in text or "log" in text:
        areas.append("user-facing/log proof surface")
    if not files:
        files.append("discover exact files with search_files before editing")
    if not areas:
        areas.append("source-code area named by the task description")
    return _dedupe(files), _dedupe(areas)


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _required_commands(goal_id: str, goal_title: str, description: str) -> list[str]:
    text = f"{goal_title} {description}".lower()
    commands = [
        "python3.13 -m pytest -o addopts='' tests/hermes_cli/test_team_os_planner_runner.py -q",
    ]
    if "watchdog" in text or "gateway_state" in text or "active_agents" in text or "stuck-busy" in text:
        commands.append(
            "python3.13 -m pytest -o addopts='' tests/hermes_cli/test_gateway_watchdog.py -q"
        )
    if "gateway" in text or "telegram" in text or "media" in text or "attachment" in text:
        commands.append(
            "python3.13 -m pytest -o addopts='' tests/gateway tests/hermes_cli -k 'media or attachment or telegram or watchdog' -q"
        )
    if "team os" in text or "planner" in text or "contract" in text:
        commands.append(
            "python3.13 -m pytest -o addopts='' tests/hermes_cli/test_team_os_planner_runner.py tests/hermes_cli/test_team_os_phase11_contracts.py -q"
        )
    commands.append(
        "python3.13 -m py_compile hermes_cli/team_os/planner_runner.py hermes_cli/team_os/cli.py"
    )
    return _dedupe(commands)


def _acceptance_subject(description: str) -> str:
    subject = " ".join(description.split())
    subject = re.sub(
        r"\s+like\s+complete[sd]?\s+(?:the|this|exact)?\s*subtask\.?",
        "",
        subject,
        flags=re.IGNORECASE,
    )
    for pattern in _GENERIC_ACCEPTANCE_PATTERNS:
        subject = pattern.sub("generic acceptance phrasing", subject)
    return subject or description


_GENERIC_ACCEPTANCE_PATTERNS = (
    re.compile(r"\bcomplete[sd]?\s+(?:the|this|exact)?\s*subtask\b", re.IGNORECASE),
    re.compile(r"\bimplementation\s+is\s+done\b", re.IGNORECASE),
    re.compile(r"\bwork\s+is\s+done\b", re.IGNORECASE),
    re.compile(r"\btask\s+is\s+complete\b", re.IGNORECASE),
)


def _crisp_acceptance_criteria(*, task_description: str, files: list[str]) -> list[str]:
    subject = _acceptance_subject(task_description)
    file_scope = ", ".join(files)
    return [
        f"Pass/fail: {subject} is satisfied by observable behavior in the changed code or artifact",
        f"Pass/fail: changed paths are limited to the declared files/areas: {file_scope}",
        "Pass/fail: focused regression proof shows RED before GREEN, or names why RED is not applicable",
        "Pass/fail: Worker handoff lists changed files, command output, and proof artifact path",
    ]


def _generic_acceptance_errors(criteria: Sequence[str], *, prefix: str) -> list[str]:
    errors: list[str] = []
    for idx, item in enumerate(criteria):
        text = str(item).strip()
        lower = text.lower()
        if any(pattern.search(text) for pattern in _GENERIC_ACCEPTANCE_PATTERNS):
            errors.append(
                f"{prefix}: generic acceptance criterion[{idx}] must be rewritten as observable pass/fail behavior"
            )
            continue
        if not lower.startswith("pass/fail:"):
            errors.append(f"{prefix}: acceptance criterion[{idx}] must start with 'Pass/fail:'")
    return errors


def _build_validation_contract(
    *,
    goal_id: str,
    goal_title: str,
    task: Any,
) -> dict[str, Any]:
    title = goal_title or goal_id
    files, areas = _infer_files_and_areas(goal_title, task.description)
    commands = _required_commands(goal_id, goal_title, task.description)
    acceptance_criteria = _crisp_acceptance_criteria(task_description=task.description, files=files)
    return {
        "role": "planner-output",
        "source_ticket": goal_id,
        "problem": title,
        "areas": areas,
        "files_to_touch": files,
        "implementation_scope": [
            f"Solve only subtask {task.task_id}: {task.description}",
            "Do prerequisite discovery with search/read tools before editing files",
            "Keep changes in the isolated development worktree until review/merge approval",
        ],
        "acceptance_criteria": acceptance_criteria,
        "proof_required": [
            "RED/GREEN focused test output or explicit reason RED is not applicable",
            "Focused pytest/compile command output with exit code",
            "git diff --stat plus changed-path summary",
            "Validator review against this contract before Done",
        ],
        "required_commands": commands,
        "intended_behavior": (
            f"Preserve source goal intent for {goal_id} ({title}) while completing "
            f"planner subtask {task.task_id}: {task.description}"
        ),
        "non_goals": [
            "Do not feed this Planner output into the loop before human review",
            "Do not auto-dispatch Developer/Worker execution from this output",
            "Do not auto-Done the Linear issue from this output",
            "Do not expand scope beyond the source goal intent or this subtask contract",
        ],
        "assertions": [
            f"Intent preserved from source goal {goal_id}: {title}",
            f"Subtask-specific acceptance criteria are satisfied: {acceptance_criteria[0]}",
            f"Worker touched only allowed files/areas: {', '.join(files)}",
            f"Required proof commands were run: {'; '.join(commands)}",
            "Human approval is recorded before any loop feed or downstream Worker execution",
        ],
        "commands": commands,
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": [
            "Planner contract no longer preserves the source goal intent",
            "Validation contract schema fails",
            "Grounding fields are missing or boilerplate-only",
            "Acceptance criteria or proof commands do not match the subtask",
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

        grounding_fields = (
            "problem",
            "areas",
            "files_to_touch",
            "implementation_scope",
            "acceptance_criteria",
            "proof_required",
            "required_commands",
        )
        for field in grounding_fields:
            value = contract.get(field)
            if field == "problem":
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{prefix}: grounding field {field!r} is missing or empty")
            elif not isinstance(value, list) or not value or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                errors.append(f"{prefix}: grounding field {field!r} must be a non-empty string list")

        required_commands = contract.get("required_commands")
        if isinstance(required_commands, list) and contract.get("commands") != required_commands:
            errors.append(f"{prefix}: contract commands must match required_commands")

        acceptance = contract.get("acceptance_criteria")
        task_description = str(planned.get("description", ""))
        if isinstance(acceptance, list):
            errors.extend(_generic_acceptance_errors(acceptance, prefix=prefix))
            if task_description:
                task_anchor = _acceptance_subject(task_description)[:32]
                if not any(task_anchor in item for item in acceptance if isinstance(item, str)):
                    errors.append(
                        f"{prefix}: acceptance criteria must reference the specific subtask description"
                    )

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


def _derive_section_tasks(
    *,
    goal_id: str,
    goal_title: str,
    goal_body: str,
    base_task: CandidateTask,
) -> list[CandidateTask] | None:
    """Split common Linear issue sections into Worker-ready planner subtasks."""
    patterns = [
        ("Desired hardening", r"Desired hardening:\s*(.*?)(?=\n\s*\n\s*Acceptance proof:|\Z)"),
        ("Acceptance proof", r"Acceptance proof:\s*(.*?)(?=\n\s*\n\s*[A-Z][A-Za-z ]+:|\Z)"),
        ("Recommended route", r"Recommended route:\s*(.*?)(?=\n\s*\n\s*Proof needed before Done:|\Z)"),
        ("Proof needed", r"Proof needed before Done:\s*(.*?)(?=\n\s*\n\s*[A-Z][A-Za-z ]+:|\Z)"),
    ]
    descriptions: list[str] = []
    for label, pattern in patterns:
        match = re.search(pattern, goal_body, flags=re.IGNORECASE | re.DOTALL)
        if match:
            text = " ".join(match.group(1).split())
            if text:
                descriptions.append(f"{label}: {text}")

    if len(descriptions) < 2:
        return None

    tasks: list[CandidateTask] = []
    for idx, description in enumerate(descriptions, start=1):
        task_id = f"{goal_id}-p{idx}"
        prereqs = (tasks[-1].task_id,) if tasks else ()
        tasks.append(
            CandidateTask(
                task_id=task_id,
                title=f"{goal_title or goal_id} — section {idx}",
                description=description,
                confidence=base_task.confidence,
                confidence_reasons=base_task.confidence_reasons,
                prerequisites=prereqs,
                approval_required=base_task.approval_required,
                reversibility_category=base_task.reversibility_category,
                reversibility_reason=base_task.reversibility_reason,
                route_hint=base_task.route_hint,
                verifier_plan=base_task.verifier_plan,
                dry_run=True,
            )
        )
    return tasks


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
    if len(tasks) == 1:
        section_tasks = _derive_section_tasks(
            goal_id=goal_id,
            goal_title=goal_title,
            goal_body=goal_body,
            base_task=tasks[0],
        )
        if section_tasks is not None:
            tasks = section_tasks[:max_tasks]
    planned_tasks: list[dict[str, Any]] = []
    excluded_items: list[dict[str, str]] = []
    for task in tasks:
        if _is_status_note(task.description):
            excluded_items.append(
                {
                    "task_id": task.task_id,
                    "reason": "status-note-not-worker-task",
                    "description": task.description,
                }
            )
            continue
        task_data = task.to_dict()
        task_data["worker_ready"] = True
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
        "excluded_items": excluded_items,
        "tasks": planned_tasks,
        "planner_review": review,
    }
