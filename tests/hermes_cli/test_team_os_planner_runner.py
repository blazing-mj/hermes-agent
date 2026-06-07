"""Team OS Planner-runner tests for AGENTS-170.

Strict TDD: tests define the Planner -> Validator intent-preservation gate before
implementation. The runner is dry-run only: it emits subtasks + validation
contracts for human review and does not feed the loop.
"""

from __future__ import annotations

import argparse
import json

import pytest


class _RootParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: ANN001
        raise AssertionError(message)


def _goal() -> dict:
    return {
        "goal_id": "AGENTS-170",
        "goal_title": "Wire Team OS Planner-runner with intent-preserving Validator gate",
        "goal_body": (
            "Step 1: Add deterministic Planner-runner output for one Linear goal.\n"
            "Step 2: Require Validator intent check before any loop feed.\n"
            "Step 3: Keep human gate ON, no auto-dispatch, and no auto-Done."
        ),
        "labels": ["system:hermes", "type:rail"],
    }


def test_plan_goal_returns_dry_run_contracts_that_require_human_review():
    from hermes_cli.team_os.planner_runner import plan_goal
    from hermes_cli.team_os.contracts import check_contract

    run = plan_goal(**_goal())

    assert run["dry_run"] is True
    assert run["loop_feed_allowed"] is False
    assert run["human_review_required"] is True
    assert run["auto_dispatch_allowed"] is False
    assert run["auto_done_allowed"] is False
    assert run["goal_id"] == "AGENTS-170"
    assert len(run["tasks"]) == 3

    for planned in run["tasks"]:
        contract = planned["validation_contract"]
        assert check_contract(contract) == []
        assert contract["source_ticket"] == "AGENTS-170"
        assert contract["human_gate_required"] is True
        assert any("intent" in item.lower() for item in contract["assertions"])
        assert any("auto-dispatch" in item.lower() for item in contract["non_goals"])
        assert any("auto-done" in item.lower() for item in contract["non_goals"])


def test_plan_goal_validator_passes_when_contracts_preserve_goal_intent():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(**_goal())

    review = run["planner_review"]
    assert review["verdict"] == "PASS"
    assert review["intent_preserved"] is True
    assert review["schema_valid"] is True
    assert review["loop_feed_allowed"] is False
    assert review["errors"] == []


def test_validate_planner_output_bounces_contract_that_loses_goal_intent():
    from hermes_cli.team_os.planner_runner import plan_goal, validate_planner_output

    run = plan_goal(**_goal())
    bad_run = json.loads(json.dumps(run))
    bad_run["tasks"][0]["title"] = "Unrelated cleanup"
    bad_run["tasks"][0]["description"] = "Rotate a color palette in a mockup."
    bad_run["tasks"][0]["validation_contract"]["intended_behavior"] = (
        "Rotate a color palette in a mockup."
    )
    bad_run["tasks"][0]["validation_contract"]["assertions"] = [
        "Mockup color palette changes are visible"
    ]

    review = validate_planner_output(
        goal_id=_goal()["goal_id"],
        goal_title=_goal()["goal_title"],
        goal_body=_goal()["goal_body"],
        planned_tasks=bad_run["tasks"],
        loop_feed_allowed=bad_run["loop_feed_allowed"],
    )

    assert review["verdict"] == "BOUNCE"
    assert review["intent_preserved"] is False
    assert any("intent" in err.lower() for err in review["errors"])


def test_validate_planner_output_bounces_task_drift_even_if_contract_boilerplate_keeps_goal():
    from hermes_cli.team_os.planner_runner import plan_goal, validate_planner_output

    run = plan_goal(**_goal())
    bad_run = json.loads(json.dumps(run))
    bad_run["tasks"][0]["title"] = "Unrelated cleanup"
    bad_run["tasks"][0]["description"] = "Rotate a color palette in a mockup."

    review = validate_planner_output(
        goal_id=_goal()["goal_id"],
        goal_title=_goal()["goal_title"],
        goal_body=_goal()["goal_body"],
        planned_tasks=bad_run["tasks"],
        loop_feed_allowed=bad_run["loop_feed_allowed"],
    )

    assert review["verdict"] == "BOUNCE"
    assert review["intent_preserved"] is False
    assert any("task intent" in err.lower() for err in review["errors"])


def test_validate_planner_output_bounces_auto_dispatch_or_auto_done_flags():
    from hermes_cli.team_os.planner_runner import plan_goal, validate_planner_output

    run = plan_goal(**_goal())

    review = validate_planner_output(
        goal_id=_goal()["goal_id"],
        goal_title=_goal()["goal_title"],
        goal_body=_goal()["goal_body"],
        planned_tasks=run["tasks"],
        loop_feed_allowed=run["loop_feed_allowed"],
        auto_dispatch_allowed=True,
        auto_done_allowed=True,
    )

    assert review["verdict"] == "BOUNCE"
    assert any("auto-dispatch" in err.lower() for err in review["errors"])
    assert any("auto-done" in err.lower() for err in review["errors"])


def test_plan_goal_contract_commands_are_non_empty_required_commands():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(**_goal())

    for planned in run["tasks"]:
        contract = planned["validation_contract"]
        assert contract["commands"]
        assert contract["commands"] == contract["required_commands"]


def test_grounded_contracts_include_worker_ready_specific_scope_and_proof():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(
        goal_id="AGENTS-148",
        goal_title="Prevent placeholder media paths from being treated as sendable attachments",
        goal_body=(
            "Step 1: Locate the Telegram/gateway media attachment parsing path and add a deterministic guard for placeholder paths.\n"
            "Step 2: Add regression tests proving placeholder strings are dropped while real existing media paths still send.\n"
            "Step 3: Update the user-facing error/proof message so discarded placeholders are visible in logs without sending files."
        ),
        labels=["system:hermes", "type:bug"],
    )

    assert run["planner_review"]["verdict"] == "PASS"
    assert len(run["tasks"]) == 3
    seen_acceptance = set()
    for task in run["tasks"]:
        contract = task["validation_contract"]
        assert contract["problem"].startswith("Prevent placeholder media paths")
        assert contract["files_to_touch"]
        assert contract["areas"]
        assert contract["implementation_scope"]
        assert contract["acceptance_criteria"]
        assert contract["proof_required"]
        assert contract["required_commands"]
        assert contract["commands"] == contract["required_commands"]
        assert any("pytest" in cmd for cmd in contract["required_commands"])
        assert any(task["description"][:32] in item for item in contract["acceptance_criteria"])
        seen_acceptance.add(tuple(contract["acceptance_criteria"]))

    assert len(seen_acceptance) == len(run["tasks"])


def test_generated_acceptance_criteria_are_crisp_pass_fail_conditions():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(
        goal_id="AGENTS-172",
        goal_title="Polish Planner acceptance criteria into crisp testable conditions",
        goal_body=(
            "Step 1: Replace generic Planner acceptance criteria with observable pass/fail conditions.\n"
            "Step 2: Validator bounces generic criteria like complete the subtask.\n"
            "Step 3: Focused tests prove generic criteria bounce and crisp criteria pass."
        ),
        labels=["system:hermes", "type:rail"],
    )

    assert run["planner_review"]["verdict"] == "PASS"
    forbidden = ("complete the subtask", "completes this exact subtask", "complete this exact subtask")
    for task in run["tasks"]:
        criteria = task["validation_contract"]["acceptance_criteria"]
        assert len(criteria) >= 3
        assert all(not any(phrase in item.lower() for phrase in forbidden) for item in criteria)
        assert all("pass/fail:" in item.lower() for item in criteria)


def test_validate_planner_output_bounces_generic_acceptance_criteria():
    from hermes_cli.team_os.planner_runner import plan_goal, validate_planner_output

    goal_body = (
        "Step 1: Replace generic Planner acceptance criteria with observable pass/fail conditions.\n"
        "Step 2: Validator bounces generic criteria like complete the subtask.\n"
        "Step 3: Focused tests prove generic criteria bounce and crisp criteria pass."
    )
    run = plan_goal(
        goal_id="AGENTS-172",
        goal_title="Polish Planner acceptance criteria into crisp testable conditions",
        goal_body=goal_body,
        labels=["system:hermes", "type:rail"],
    )
    bad_run = json.loads(json.dumps(run))
    task = bad_run["tasks"][0]
    task["validation_contract"]["acceptance_criteria"] = [
        f"Complete the subtask: {task['description']}",
        "Implementation is done",
    ]

    review = validate_planner_output(
        goal_id="AGENTS-172",
        goal_title="Polish Planner acceptance criteria into crisp testable conditions",
        goal_body=goal_body,
        planned_tasks=bad_run["tasks"],
        loop_feed_allowed=bad_run["loop_feed_allowed"],
    )

    assert review["verdict"] == "BOUNCE"
    assert any("generic acceptance" in err.lower() for err in review["errors"])


def test_generated_acceptance_criteria_strip_generic_examples_from_task_text():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(
        goal_id="AGENTS-172",
        goal_title="Polish Planner acceptance criteria into crisp testable conditions",
        goal_body=(
            "Step 1: Replace acceptance criteria that say complete the subtask with observable pass/fail conditions.\n"
            "Step 2: Keep the Planner output validator strict."
        ),
        labels=["system:hermes", "type:rail"],
    )

    assert run["planner_review"]["verdict"] == "PASS"
    all_criteria = "\n".join(
        item
        for task in run["tasks"]
        for item in task["validation_contract"]["acceptance_criteria"]
    ).lower()
    assert "complete the subtask" not in all_criteria


def test_status_note_phase_is_excluded_from_worker_tasks():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(
        goal_id="AGENTS-170",
        goal_title="Ground Planner-runner output for Worker handoff",
        goal_body=(
            "Step 1: Add grounded contracts with files, scope, acceptance criteria, and proof commands.\n"
            "Step 2: Re-prove on a normal non-self Linear goal.\n"
            "Step 3: Keep status hygiene accurate: AGENTS-167 is Done and PAT rotation remains pending."
        ),
        labels=["system:hermes", "type:rail"],
    )

    assert [task["task_id"] for task in run["tasks"]] == ["AGENTS-170-p1", "AGENTS-170-p2"]
    assert run["excluded_items"] == [
        {
            "task_id": "AGENTS-170-p3",
            "reason": "status-note-not-worker-task",
            "description": "Keep status hygiene accurate: AGENTS-167 is Done and PAT rotation remains pending.",
        }
    ]


def test_sectioned_real_issue_body_produces_worker_subtasks_not_single_blob():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(
        goal_id="AGENTS-161",
        goal_title="Gateway watchdog should detect stale active-agent hung state",
        goal_body=(
            "Follow-up from AGENTS-160. Current safe fix guards active_agents > 0 to avoid killing busy gateways.\n\n"
            "Desired hardening: compare ~/.hermes/gateway_state.json updated_at/mtime against a larger stuck-busy threshold. "
            "If active_agents > 0 and heartbeat has not advanced for N minutes, classify as stuck-busy and escalate/recover via a stricter policy.\n\n"
            "Acceptance proof: RED/GREEN tests for recent-active suppress, stale-active detect/escalate, normal healthy reset, "
            "and live-safe scheduled watchdog proof that does not kill a healthy active gateway."
        ),
        labels=["system:hermes", "type:ops"],
    )

    assert [task["task_id"] for task in run["tasks"]] == ["AGENTS-161-p1", "AGENTS-161-p2"]
    assert "stuck-busy threshold" in run["tasks"][0]["description"]
    assert "RED/GREEN tests" in run["tasks"][1]["description"]
    assert all(task["worker_ready"] is True for task in run["tasks"])
    assert "hermes_cli/gateway_watchdog.py" in run["tasks"][0]["validation_contract"]["files_to_touch"]
    assert "tests/hermes_cli/test_gateway_watchdog.py" in run["tasks"][1]["validation_contract"]["files_to_touch"]


def test_plan_goal_cli_writes_reviewable_output_without_loop_feed(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "planner-run.json"
    goal = _goal()
    args = argparse.Namespace(
        team_os_command="plan-goal",
        goal_id=goal["goal_id"],
        goal_title=goal["goal_title"],
        goal_body=goal["goal_body"],
        label=goal["labels"],
        max_tasks=10,
        output=str(output),
    )

    rc = cmd_team_os(args)

    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["planner_review"]["verdict"] == "PASS"
    assert data["loop_feed_allowed"] is False
    assert data["human_review_required"] is True
    assert all("validation_contract" in task for task in data["tasks"])


def test_plan_goal_subcommand_is_registered_on_team_os_parser(tmp_path):
    from hermes_cli.team_os.cli import register_cli

    output = tmp_path / "planner-run.json"
    goal = _goal()
    root = _RootParser(prog="hermes")
    team_os = root.add_subparsers(dest="command").add_parser("team-os")
    register_cli(team_os)

    args = root.parse_args(
        [
            "team-os",
            "plan-goal",
            goal["goal_id"],
            "--goal-title",
            goal["goal_title"],
            "--goal-body",
            goal["goal_body"],
            "--label",
            "system:hermes",
            "--output",
            str(output),
        ]
    )

    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["dry_run"] is True
    assert data["planner_review"]["verdict"] == "PASS"


def test_plan_goal_parser_propagates_bounce_exit_code(tmp_path):
    from hermes_cli.team_os.cli import register_cli

    output = tmp_path / "planner-run.json"
    root = _RootParser(prog="hermes")
    team_os = root.add_subparsers(dest="command").add_parser("team-os")
    register_cli(team_os)

    args = root.parse_args(
        [
            "team-os",
            "plan-goal",
            "AGENTS-170",
            "--goal-title",
            "",
            "--goal-body",
            "Maybe do something, not sure.",
            "--output",
            str(output),
        ]
    )

    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["planner_review"]["verdict"] == "BOUNCE"
    assert data["loop_feed_allowed"] is False


def test_plan_goal_cli_returns_nonzero_when_intent_review_bounces(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "planner-run.json"
    args = argparse.Namespace(
        team_os_command="plan-goal",
        goal_id="AGENTS-170",
        goal_title="",
        goal_body="Maybe do something, not sure.",
        label=[],
        max_tasks=10,
        output=str(output),
    )

    rc = cmd_team_os(args)

    assert rc == 1
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["planner_review"]["verdict"] == "BOUNCE"
    assert data["loop_feed_allowed"] is False
    assert any("intent" in err.lower() for err in data["planner_review"]["errors"])
