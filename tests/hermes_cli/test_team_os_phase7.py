"""Phase 7 Telegram approval delivery rail tests for hermes team-os.

Strict TDD: these specs are written before the delivery rail exists.

Boundaries enforced:
    * Delivery objects are pure data; they never reach the network.
    * dry_run defaults to True; production execution requires explicit opt-in.
    * Persistence is opt-in via --state-db; CLI runs are otherwise read-only.
    * The loop runner stays blocked while an approval is pending.
"""

from __future__ import annotations

import json
from argparse import Namespace


def test_delivery_construction_from_sample():
    from hermes_cli.team_os.approvals import build_approval_sample
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery

    sample = build_approval_sample(
        task_id="AGENTS-99",
        title="Phase 7 delivery",
        action="run database migration",
    )

    delivery = TelegramApprovalDelivery.from_approval_sample(
        sample, approval_id=17, dry_run=True,
    )

    assert delivery.task_id == "AGENTS-99"
    assert delivery.title == "Phase 7 delivery"
    assert delivery.action == "run database migration"
    assert delivery.approval_id == 17
    assert delivery.dry_run is True
    assert len(delivery.plan_summary) == 3
    assert delivery.prompt == sample.prompt
    assert delivery.rollback_path == sample.rollback_path
    assert delivery.risk_if_wrong == sample.risk_if_wrong


def test_delivery_result_to_dict():
    from hermes_cli.team_os.delivery import ApprovalDeliveryResult

    result = ApprovalDeliveryResult(
        approval_id=5,
        dry_run=True,
        delivered=False,
        message_id=None,
        error=None,
    )
    data = result.to_dict()
    assert data == {
        "approval_id": 5,
        "dry_run": True,
        "delivered": False,
        "message_id": None,
        "error": None,
    }


def test_build_delivery_from_sample_persists_to_db(tmp_path):
    from hermes_cli.team_os.approvals import build_approval_sample
    from hermes_cli.team_os.db import TeamOSState
    from hermes_cli.team_os.delivery import build_delivery_from_sample

    db = TeamOSState(tmp_path / "team-os.db")
    sample = build_approval_sample(
        task_id="AGENTS-100",
        title="Delivery persistence",
        action="run database migration 003",
    )

    delivery = build_delivery_from_sample(sample, db, dry_run=True)

    assert delivery.approval_id > 0
    assert delivery.dry_run is True
    record = db.get_approval_request(delivery.approval_id)
    assert record["task_id"] == "AGENTS-100"
    assert record["status"] == "pending"
    assert record["prompt"] == sample.prompt


def test_is_blocked_without_approval_true_for_pending():
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery

    assert TelegramApprovalDelivery.is_blocked_without_approval("pending") is True
    assert TelegramApprovalDelivery.is_blocked_without_approval("deferred") is True
    assert TelegramApprovalDelivery.is_blocked_without_approval("cancelled") is True


def test_is_blocked_without_approval_false_for_approved():
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery

    assert TelegramApprovalDelivery.is_blocked_without_approval("approved") is False
    assert TelegramApprovalDelivery.is_blocked_without_approval("auto-approved") is False


def test_is_blocked_without_approval_false_for_none():
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery

    assert TelegramApprovalDelivery.is_blocked_without_approval(None) is False


def test_cli_deliver_approval_dry_run_no_db(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "delivery.json"
    args = Namespace(
        team_os_command="deliver-approval",
        task_id="AGENTS-101",
        title="Deliver dry run",
        action="run database migration",
        state_db=None,
        output=str(output),
        dry_run=True,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_id"] == "AGENTS-101"
    assert data["approval_id"] == 0
    assert data["dry_run"] is True
    assert "/approve" in data["prompt"]


def test_cli_deliver_approval_with_state_db(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.db import TeamOSState

    db_path = tmp_path / "team-os.db"
    output = tmp_path / "delivery.json"
    args = Namespace(
        team_os_command="deliver-approval",
        task_id="AGENTS-102",
        title="Deliver persisted",
        action="run database migration",
        state_db=str(db_path),
        output=str(output),
        dry_run=True,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_id"] == "AGENTS-102"
    assert data["approval_id"] > 0
    record = TeamOSState(db_path).get_approval_request(data["approval_id"])
    assert record["task_id"] == "AGENTS-102"
    assert record["status"] == "pending"


def test_loop_runner_blocks_pending_approval():
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="needs-approval",
            title="awaiting approval",
            priority=100,
            approval_status="pending",
            quota_confidence="high",
        ),
        LoopTask(
            task_id="ready",
            title="ready to run",
            priority=1,
            approval_status="approved",
            quota_confidence="high",
        ),
    ]

    decision = select_next_task(tasks, current_shift="day")

    assert decision.selected_task_id == "ready"
    assert "needs-approval" in decision.skipped_task_ids
    assert decision.skip_reasons["needs-approval"] == "approval pending"
    assert TelegramApprovalDelivery.is_blocked_without_approval("pending") is True
