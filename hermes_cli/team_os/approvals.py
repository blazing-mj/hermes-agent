"""Approval and reversibility primitives for Team OS Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReversibilityCategory(Enum):
    FULL_INSTANT = "full-instant"
    FULL_EFFORT = "full-effort"
    DATA_MIGRATION = "data-migration"
    CREDENTIAL_CHANGE = "credential-change"
    EXTERNAL_SIDE_EFFECT = "external-side-effect"
    MASS_DELETE = "mass-delete"
    NONE = "none"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"
    AUTO_APPROVED = "auto-approved"


_MANUAL_APPROVAL_CATEGORIES = {
    ReversibilityCategory.DATA_MIGRATION,
    ReversibilityCategory.CREDENTIAL_CHANGE,
    ReversibilityCategory.EXTERNAL_SIDE_EFFECT,
    ReversibilityCategory.MASS_DELETE,
    ReversibilityCategory.NONE,
}


@dataclass(frozen=True)
class ReversibilityAssessment:
    category: ReversibilityCategory
    reason: str
    requires_manual_approval: bool
    auto_approval_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "reason": self.reason,
            "requires_manual_approval": self.requires_manual_approval,
            "auto_approval_allowed": self.auto_approval_allowed,
        }


@dataclass(frozen=True)
class ApprovalSample:
    task_id: str
    title: str
    action: str
    reversibility: ReversibilityAssessment
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "action": self.action,
            "reversibility": self.reversibility.to_dict(),
            "requires_manual_approval": self.reversibility.requires_manual_approval,
            "prompt": self.prompt,
        }


def _manual(category: ReversibilityCategory, reason: str) -> ReversibilityAssessment:
    return ReversibilityAssessment(
        category=category,
        reason=reason,
        requires_manual_approval=category in _MANUAL_APPROVAL_CATEGORIES,
        auto_approval_allowed=category not in _MANUAL_APPROVAL_CATEGORIES,
    )


def classify_reversibility(action: str) -> ReversibilityAssessment:
    """Classify an action's reversibility using conservative keyword rules.

    Phase 2 uses deterministic rails only; no LLM guessing.  Ambiguous or
    irreversible operations fall to manual approval rather than auto-run.
    """

    text = action.lower()
    if any(token in text for token in ("migration", "migrate", "schema", "database", "db ")):
        return _manual(
            ReversibilityCategory.DATA_MIGRATION,
            "database/schema/data migration requires explicit approval",
        )
    if any(token in text for token in ("api key", "secret", "credential", "token", "password")):
        return _manual(
            ReversibilityCategory.CREDENTIAL_CHANGE,
            "credential or secret change is not safely auto-reversible",
        )
    if any(token in text for token in ("send email", "post tweet", "publish", "deploy production", "charge ")):
        return _manual(
            ReversibilityCategory.EXTERNAL_SIDE_EFFECT,
            "external side effect requires explicit approval",
        )
    if any(token in text for token in ("mass delete", "delete all", "purge", "drop table", "rm -rf")):
        return _manual(
            ReversibilityCategory.MASS_DELETE,
            "mass deletion is high-risk and requires explicit approval",
        )
    if any(token in text for token in ("irreversible", "cannot rollback", "no rollback")):
        return _manual(
            ReversibilityCategory.NONE,
            "declared irreversible action requires explicit approval",
        )
    if any(token in text for token in ("refactor", "rename", "multi-file", "large edit")):
        return _manual(
            ReversibilityCategory.FULL_EFFORT,
            "reversible with effort; approval depends on caller policy",
        )
    return _manual(
        ReversibilityCategory.FULL_INSTANT,
        "local file change appears instantly reversible via git/checkpoint",
    )


def render_approval_prompt(
    *,
    task_id: str,
    title: str,
    action: str,
    category: ReversibilityCategory,
    reason: str,
) -> str:
    """Render the human approval prompt before any Telegram delivery rail."""

    return "\n".join(
        [
            f"Approval needed: {task_id} — {title}",
            f"Action: {action}",
            f"Reversibility: {category.value}",
            f"Reason: {reason}",
            "",
            "Choose one:",
            f"/approve {task_id}",
            f"/reject {task_id} <reason>",
            f"/defer {task_id} <until/why>",
            f"/approve-modified {task_id} <modified scope>",
        ]
    )


def build_approval_sample(*, task_id: str, title: str, action: str) -> ApprovalSample:
    assessment = classify_reversibility(action)
    prompt = render_approval_prompt(
        task_id=task_id,
        title=title,
        action=action,
        category=assessment.category,
        reason=assessment.reason,
    )
    return ApprovalSample(
        task_id=task_id,
        title=title,
        action=action,
        reversibility=assessment,
        prompt=prompt,
    )


def decision_to_status(decision: str) -> ApprovalStatus:
    normalized = decision.strip().lower().replace("_", "-")
    if normalized in {"approve", "approved"}:
        return ApprovalStatus.APPROVED
    if normalized in {"approve-modified", "approved-modified"}:
        return ApprovalStatus.APPROVED
    if normalized in {"defer", "deferred"}:
        return ApprovalStatus.DEFERRED
    if normalized in {"reject", "rejected", "deny", "denied"}:
        return ApprovalStatus.CANCELLED
    raise ValueError(f"unknown approval decision: {decision}")
