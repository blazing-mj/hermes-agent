from __future__ import annotations


def test_failure_cost_high_and_critical_require_mj_review():
    from hermes_cli.team_os.approvals import ReversibilityCategory
    from hermes_cli.team_os.failure_cost import assess_failure_cost

    assert assess_failure_cost(ReversibilityCategory.EXTERNAL_SIDE_EFFECT).requires_mj_review is True
    assert assess_failure_cost(ReversibilityCategory.CREDENTIAL_CHANGE).tier == "critical"


def test_failure_cost_low_medium_stay_gated_queue():
    from hermes_cli.team_os.approvals import ReversibilityCategory
    from hermes_cli.team_os.failure_cost import assess_failure_cost

    assert assess_failure_cost(ReversibilityCategory.FULL_INSTANT).requires_mj_review is False
    assert assess_failure_cost(ReversibilityCategory.FULL_EFFORT).tier == "medium"
