import json


def test_reversibility_classifier_full_instant_auto_approves():
    from hermes_cli.team_os.approvals import ReversibilityCategory, classify_reversibility

    result = classify_reversibility("format README.md and reorder imports")

    assert result.category is ReversibilityCategory.FULL_INSTANT
    assert result.requires_manual_approval is False
    assert result.auto_approval_allowed is True


def test_reversibility_classifier_data_migration_blocks():
    from hermes_cli.team_os.approvals import ReversibilityCategory, classify_reversibility

    result = classify_reversibility("run irreversible database migration on production")

    assert result.category is ReversibilityCategory.DATA_MIGRATION
    assert result.requires_manual_approval is True
    assert result.auto_approval_allowed is False


def test_rejection_cancels_approval_record(tmp_path):
    from hermes_cli.team_os.approvals import ApprovalStatus, ReversibilityCategory
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(tmp_path / "team-os.db")
    request_id = state.create_approval_request(
        task_id="AGENTS-68",
        title="dangerous migration",
        action="run migration",
        reversibility_category=ReversibilityCategory.DATA_MIGRATION,
        reversibility_reason="database migration requires explicit approval",
        prompt="prompt text",
    )

    updated = state.record_approval_decision(
        request_id,
        decision="reject",
        actor="MJ",
        reason="too risky",
    )

    assert updated["status"] == ApprovalStatus.CANCELLED.value
    assert updated["decision"] == "reject"
    assert updated["actor"] == "MJ"


def test_approval_prompt_renderer_contains_full_decision_context():
    from hermes_cli.team_os.approvals import ReversibilityCategory, render_approval_prompt

    prompt = render_approval_prompt(
        task_id="AGENTS-69",
        title="Apply production DB migration",
        action="run migration 002",
        why="Adds the approvals table needed before Telegram approval delivery.",
        why_now="Phase 7 is blocked until production approval prompts carry full context.",
        what_if_no="Rejecting keeps Telegram approval delivery blocked and leaves local samples only.",
        category=ReversibilityCategory.DATA_MIGRATION,
        reason="schema/data migration is not instantly reversible",
        rollback_path="Restore the pre-migration database backup and revert the migration commit.",
        risk_if_wrong="Bad schema could corrupt approval history or block future /approve decisions.",
        plan_summary=(
            "Back up the Team OS database",
            "Apply migration 002 in a maintenance window",
            "Run readback checks against the approvals table",
        ),
    )

    expected_fields = [
        "What:",
        "Why:",
        "Why now:",
        "What if no:",
        "Reversibility:",
        "Risk if wrong:",
        "Plan summary:",
    ]
    for field in expected_fields:
        assert field in prompt

    assert "AGENTS-69" in prompt
    assert "data-migration" in prompt
    assert "Restore the pre-migration database backup" in prompt
    assert "Bad schema could corrupt approval history" in prompt
    assert "1. Back up the Team OS database" in prompt
    assert "2. Apply migration 002 in a maintenance window" in prompt
    assert "3. Run readback checks against the approvals table" in prompt
    assert "/approve" in prompt
    assert "/reject" in prompt
    assert "/defer" in prompt
    assert "/approve-modified" in prompt


def test_approval_sample_cli_writes_prompt_json(tmp_path):
    from argparse import Namespace
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "approval-sample.json"
    args = Namespace(
        team_os_command="approval-sample",
        task_id="AGENTS-68",
        title="Apply production DB migration",
        action="run migration 002",
        output=str(output),
    )

    rc = cmd_team_os(args)

    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_id"] == "AGENTS-68"
    assert data["reversibility"]["category"] == "data-migration"
    assert data["requires_manual_approval"] is True
    assert data["why"]
    assert data["why_now"]
    assert data["what_if_no"]
    assert data["rollback_path"]
    assert data["risk_if_wrong"]
    assert len(data["plan_summary"]) == 3
    for field in ("What:", "Why:", "Why now:", "What if no:", "Risk if wrong:", "Plan summary:"):
        assert field in data["prompt"]
    assert "/approve-modified" in data["prompt"]
