"""Failure-cost policy for Team OS Stage 4.

The map is deterministic policy, not an LLM judgment.  It decides whether a
Cortex-routed item may remain in the gated/dry-run queue or must be escalated to
MJ review before any Done/dispatch transition.
"""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import ReversibilityCategory


@dataclass(frozen=True)
class FailureCostDecision:
    category: ReversibilityCategory
    tier: str
    requires_mj_review: bool
    reason: str


_FAILURE_COST_TIERS: dict[ReversibilityCategory, str] = {
    ReversibilityCategory.FULL_INSTANT: "low",
    ReversibilityCategory.FULL_EFFORT: "medium",
    ReversibilityCategory.EXTERNAL_SIDE_EFFECT: "high",
    ReversibilityCategory.DATA_MIGRATION: "high",
    ReversibilityCategory.CREDENTIAL_CHANGE: "critical",
    ReversibilityCategory.MASS_DELETE: "critical",
    ReversibilityCategory.NONE: "critical",
}


def assess_failure_cost(category: ReversibilityCategory) -> FailureCostDecision:
    """Return the deterministic failure-cost decision for ``category``."""
    tier = _FAILURE_COST_TIERS[category]
    requires = tier in {"high", "critical"}
    reason = (
        f"{category.value} maps to {tier} failure cost; MJ review required"
        if requires
        else f"{category.value} maps to {tier} failure cost; gated queue may proceed"
    )
    return FailureCostDecision(
        category=category,
        tier=tier,
        requires_mj_review=requires,
        reason=reason,
    )
