"""Telegram approval delivery rail for Team OS Phase 7.

Pure data objects only — no network calls.  ``TelegramApprovalDelivery``
captures the seven-field decision context (action, why, why-now, what-if-no,
rollback path, risk-if-wrong, plan summary) and the persisted approval id;
the Telegram adapter consumes it to render the inline keyboard.  All new code
defaults to ``dry_run=True``; production execution requires explicit opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .approvals import ApprovalSample


_APPROVED_STATUSES = {None, "approved", "auto-approved"}


@dataclass(frozen=True)
class TelegramApprovalDelivery:
    task_id: str
    title: str
    action: str
    why: str
    why_now: str
    what_if_no: str
    rollback_path: str
    risk_if_wrong: str
    plan_summary: tuple[str, str, str]
    approval_id: int
    prompt: str
    dry_run: bool = True

    @classmethod
    def from_approval_sample(
        cls,
        sample: ApprovalSample,
        *,
        approval_id: int,
        dry_run: bool = True,
    ) -> "TelegramApprovalDelivery":
        return cls(
            task_id=sample.task_id,
            title=sample.title,
            action=sample.action,
            why=sample.why,
            why_now=sample.why_now,
            what_if_no=sample.what_if_no,
            rollback_path=sample.rollback_path,
            risk_if_wrong=sample.risk_if_wrong,
            plan_summary=sample.plan_summary,
            approval_id=approval_id,
            prompt=sample.prompt,
            dry_run=dry_run,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "action": self.action,
            "why": self.why,
            "why_now": self.why_now,
            "what_if_no": self.what_if_no,
            "rollback_path": self.rollback_path,
            "risk_if_wrong": self.risk_if_wrong,
            "plan_summary": list(self.plan_summary),
            "approval_id": self.approval_id,
            "prompt": self.prompt,
            "dry_run": self.dry_run,
        }

    @staticmethod
    def is_blocked_without_approval(approval_status: str | None) -> bool:
        """Return True when ``approval_status`` blocks downstream execution."""
        return approval_status not in _APPROVED_STATUSES


@dataclass(frozen=True)
class ApprovalDeliveryResult:
    approval_id: int
    dry_run: bool
    delivered: bool
    message_id: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "dry_run": self.dry_run,
            "delivered": self.delivered,
            "message_id": self.message_id,
            "error": self.error,
        }


def build_delivery_from_sample(
    sample: ApprovalSample,
    db: Any,
    *,
    dry_run: bool = True,
) -> TelegramApprovalDelivery:
    """Persist an approval request and return the matching delivery payload."""
    approval_id = db.create_approval_request(
        task_id=sample.task_id,
        title=sample.title,
        action=sample.action,
        reversibility_category=sample.reversibility.category,
        reversibility_reason=sample.reversibility.reason,
        prompt=sample.prompt,
    )
    return TelegramApprovalDelivery.from_approval_sample(
        sample, approval_id=approval_id, dry_run=dry_run,
    )
