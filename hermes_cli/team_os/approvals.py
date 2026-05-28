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
    why: str
    why_now: str
    what_if_no: str
    reversibility: ReversibilityAssessment
    rollback_path: str
    risk_if_wrong: str
    plan_summary: tuple[str, str, str]
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "action": self.action,
            "what": self.action,
            "why": self.why,
            "why_now": self.why_now,
            "what_if_no": self.what_if_no,
            "reversibility": self.reversibility.to_dict(),
            "rollback_path": self.rollback_path,
            "risk_if_wrong": self.risk_if_wrong,
            "plan_summary": list(self.plan_summary),
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
    why: str,
    why_now: str,
    what_if_no: str,
    category: ReversibilityCategory,
    reason: str,
    rollback_path: str,
    risk_if_wrong: str,
    plan_summary: tuple[str, str, str] | list[str],
) -> str:
    """Render the full v6 human approval prompt before any Telegram delivery rail."""

    if len(plan_summary) != 3:
        raise ValueError("approval plan_summary must contain exactly 3 bullets")

    return "\n".join(
        [
            f"Approval needed: {task_id} — {title}",
            f"What: {action}",
            f"Why: {why}",
            f"Why now: {why_now}",
            f"What if no: {what_if_no}",
            f"Reversibility: {category.value} — {reason}; rollback: {rollback_path}",
            f"Risk if wrong: {risk_if_wrong}",
            "Plan summary:",
            f"1. {plan_summary[0]}",
            f"2. {plan_summary[1]}",
            f"3. {plan_summary[2]}",
            "",
            "Choose one:",
            f"/approve {task_id}",
            f"/reject {task_id} <reason>",
            f"/defer {task_id} <until/why>",
            f"/approve-modified {task_id} <modified scope>",
        ]
    )


def _rollback_path_for(category: ReversibilityCategory) -> str:
    if category is ReversibilityCategory.FULL_INSTANT:
        return "Revert the local git/checkpoint change immediately."
    if category is ReversibilityCategory.FULL_EFFORT:
        return "Revert the commit/worktree change and rerun affected tests."
    if category is ReversibilityCategory.DATA_MIGRATION:
        return "Restore the pre-change data backup or apply the paired down migration, then verify readback."
    if category is ReversibilityCategory.CREDENTIAL_CHANGE:
        return "Restore the previous credential value from the approved secret store and restart affected services."
    if category is ReversibilityCategory.EXTERNAL_SIDE_EFFECT:
        return "Stop follow-on actions and apply the service-specific undo/remediation path; external recipients may still have seen it."
    if category is ReversibilityCategory.MASS_DELETE:
        return "Restore from backup/trash/snapshot and verify item counts before continuing."
    return "No reliable rollback path; cancellation before execution is the only safe reversal."


def build_approval_sample(*, task_id: str, title: str, action: str) -> ApprovalSample:
    assessment = classify_reversibility(action)
    why = "This action is the next gated step for the Team OS rollout and needs an explicit decision before execution."
    why_now = "A downstream delivery rail is blocked until this approval decision is resolved with full context."
    what_if_no = "The action will not run; the dependent phase stays blocked and the current safe state is preserved."
    rollback_path = _rollback_path_for(assessment.category)
    risk_if_wrong = f"If approved incorrectly, this {assessment.category.value} action could create cleanup work or block the rollout path."
    plan_summary = (
        "Confirm the exact scope and reversibility category",
        "Execute only the approved action with existing safety gates",
        "Run verifier/readback checks and record proof before closing",
    )
    prompt = render_approval_prompt(
        task_id=task_id,
        title=title,
        action=action,
        why=why,
        why_now=why_now,
        what_if_no=what_if_no,
        category=assessment.category,
        reason=assessment.reason,
        rollback_path=rollback_path,
        risk_if_wrong=risk_if_wrong,
        plan_summary=plan_summary,
    )
    return ApprovalSample(
        task_id=task_id,
        title=title,
        action=action,
        why=why,
        why_now=why_now,
        what_if_no=what_if_no,
        reversibility=assessment,
        rollback_path=rollback_path,
        risk_if_wrong=risk_if_wrong,
        plan_summary=plan_summary,
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
