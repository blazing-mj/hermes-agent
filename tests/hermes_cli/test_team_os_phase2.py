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


def test_approval_prompt_renderer_contains_reversibility_controls():
    from hermes_cli.team_os.approvals import ReversibilityCategory, render_approval_prompt

    prompt = render_approval_prompt(
        task_id="AGENTS-68",
        title="Apply production DB migration",
        action="run migration 002",
        category=ReversibilityCategory.DATA_MIGRATION,
        reason="schema/data migration is not instantly reversible",
    )

    assert "AGENTS-68" in prompt
    assert "data-migration" in prompt
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
    assert "/approve-modified" in data["prompt"]
