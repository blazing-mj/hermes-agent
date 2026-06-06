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


def test_plan_goal_contract_commands_are_non_empty_verifier_plans():
    from hermes_cli.team_os.planner_runner import plan_goal

    run = plan_goal(**_goal())

    for planned in run["tasks"]:
        contract = planned["validation_contract"]
        assert contract["commands"]
        assert contract["commands"] == planned["verifier_plan"]


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
