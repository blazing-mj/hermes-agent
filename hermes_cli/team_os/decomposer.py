"""Goal decomposer for Team OS Phase 8.

Deterministic, offline, no LLM calls.

Confidence scoring rules (conservative / fail-closed):
    * high   — title non-empty + body non-ambiguous + labels present + body non-sparse
    * medium — title non-empty with partial but assessable context
    * low    — title present but body is empty or contains ambiguous markers
    * unknown — title is empty

Route-hint rules:
    * unknown/none confidence -> route_hint = "none"  (fail-closed)
    * labels match heavy code types -> "claude-max"
    * all others -> "claude-max"  (default safe choice)

Multi-step detection:
    Lines matching "Phase N:", "Step N:", or leading numbered bullet "N." in the
    body are used to split the goal into per-phase CandidateTask objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .schema import VALID_CONFIDENCE

VALID_ROUTE_HINTS = {"claude-max", "codex", "none"}

# Regex for whole-word ambiguity matches (avoids false positives like "fetch"→"etc").
_AMBIGUOUS_RE = re.compile(
    r"\b(?:tbd|unclear|to be determined|maybe|possibly|not sure|might|could be|etc)\b",
    re.IGNORECASE,
)

_HEAVY_LABELS = {
    "type:code", "type:implementation", "type:build", "type:refactor",
    "type:review", "type:test", "type:fix", "type:bug",
}

# Pattern for numbered phases / steps in the body.
_PHASE_PATTERN = re.compile(
    r"^(?:phase\s+\d+|step\s+\d+|\d+\.)\s*:?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class CandidateTask:
    """A single decomposed sub-task produced by the goal decomposer."""

    task_id: str
    title: str
    description: str
    confidence: str
    confidence_reasons: tuple[str, ...]
    prerequisites: tuple[str, ...]
    approval_required: bool
    reversibility_category: str
    reversibility_reason: str
    route_hint: str
    verifier_plan: tuple[str, ...]
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.confidence not in VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got {self.confidence!r}"
            )
        if self.route_hint not in VALID_ROUTE_HINTS:
            raise ValueError(
                f"route_hint must be one of {sorted(VALID_ROUTE_HINTS)}, got {self.route_hint!r}"
            )
        if not self.confidence_reasons:
            raise ValueError("confidence_reasons must be non-empty")
        if not self.verifier_plan:
            raise ValueError("verifier_plan must be non-empty")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "confidence": self.confidence,
            "confidence_reasons": list(self.confidence_reasons),
            "prerequisites": list(self.prerequisites),
            "approval_required": self.approval_required,
            "reversibility_category": self.reversibility_category,
            "reversibility_reason": self.reversibility_reason,
            "route_hint": self.route_hint,
            "verifier_plan": list(self.verifier_plan),
            "dry_run": self.dry_run,
        }


def _score_confidence(
    *,
    title: str,
    body: str,
    labels: Sequence[str],
) -> tuple[str, list[str]]:
    """Return (confidence_level, reasons)."""

    title = title.strip()
    body = body.strip()
    reasons: list[str] = []

    if not title:
        return "unknown", ["title is empty — cannot assess confidence without a title"]

    reasons.append(f"title is non-empty: {title!r}")

    # Check for ambiguity markers in body (whole-word to avoid false positives).
    ambiguous_found = _AMBIGUOUS_RE.findall(body)
    if ambiguous_found:
        reasons.append(f"body contains ambiguous markers: {ambiguous_found}")
        return "low", reasons

    has_labels = bool(labels)
    if has_labels:
        reasons.append(f"labels present: {list(labels)}")

    body_is_empty = len(body) == 0
    body_is_sparse = len(body) < 20

    if body_is_empty:
        reasons.append("body is empty — too sparse to assess scope")
        return "low", reasons

    if body_is_sparse:
        reasons.append("body is sparse (< 20 characters)")

    if has_labels and not body_is_sparse:
        return "high", reasons

    if not has_labels:
        reasons.append("no labels provided")
    return "medium", reasons


def _route_hint_for(confidence: str, labels: Sequence[str]) -> str:
    if confidence in {"unknown", "none"}:
        return "none"
    # Heavy code labels -> claude-max
    label_set = {label.lower() for label in labels}
    if label_set & _HEAVY_LABELS:
        return "claude-max"
    return "claude-max"  # default safe choice


def _verifier_plan_for(task_title: str) -> tuple[str, ...]:
    return (
        f"Run focused pytest for {task_title!r}",
        "Verify imports compile without error",
        "Confirm dry_run=True in output artifact",
    )


def _split_phases(body: str) -> list[str] | None:
    """Return list of per-phase descriptions or None if no phase markers found."""
    matches = _PHASE_PATTERN.findall(body)
    if len(matches) >= 2:
        return [m.strip() for m in matches]
    return None


def decompose_goal(
    *,
    goal_id: str,
    goal_title: str,
    goal_body: str,
    labels: Sequence[str],
    max_tasks: int = 10,
) -> list[CandidateTask]:
    """Decompose a goal into CandidateTask objects.

    Rules:
    * If body has multi-phase structure, produce one task per phase.
    * Otherwise produce a single task.
    * All tasks have dry_run=True.
    * Chain prerequisites linearly.
    * Cap output at max_tasks.
    """

    phases = _split_phases(goal_body)

    if phases:
        # Truncate to max_tasks.
        phases = phases[:max_tasks]
        tasks: list[CandidateTask] = []
        for idx, phase_desc in enumerate(phases):
            task_id = f"{goal_id}-p{idx + 1}"
            confidence, reasons = _score_confidence(
                title=goal_title,
                body=phase_desc,
                labels=labels,
            )
            route = _route_hint_for(confidence, labels)
            prereqs = (tasks[idx - 1].task_id,) if idx > 0 else ()
            tasks.append(
                CandidateTask(
                    task_id=task_id,
                    title=f"{goal_title} — phase {idx + 1}",
                    description=phase_desc,
                    confidence=confidence,
                    confidence_reasons=tuple(reasons),
                    prerequisites=prereqs,
                    approval_required=False,
                    reversibility_category="full-instant",
                    reversibility_reason="code change reversible via git",
                    route_hint=route,
                    verifier_plan=_verifier_plan_for(f"{goal_title} phase {idx + 1}"),
                    dry_run=True,
                )
            )
        return tasks

    # Single task.
    task_id = f"{goal_id}-p1"
    confidence, reasons = _score_confidence(
        title=goal_title, body=goal_body, labels=labels
    )
    route = _route_hint_for(confidence, labels)
    return [
        CandidateTask(
            task_id=task_id,
            title=goal_title or goal_id,
            description=goal_body or goal_title or goal_id,
            confidence=confidence,
            confidence_reasons=tuple(reasons),
            prerequisites=(),
            approval_required=False,
            reversibility_category="full-instant",
            reversibility_reason="code change reversible via git",
            route_hint=route,
            verifier_plan=_verifier_plan_for(goal_title or goal_id),
            dry_run=True,
        )
    ]
