"""Phase 11 validation-contract checker and handoff template tests."""

import argparse
import json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_contract(**overrides):
    base = {
        "intended_behavior": "Run the verification gate and collect a proof artifact",
        "non_goals": ["Do not deploy to production"],
        "assertions": ["Exit code is 0", "Proof artifact exists"],
        "commands": ["pytest tests/hermes_cli/test_team_os_phase11_contracts.py"],
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": False,
        "bounce_conditions": ["Test suite fails"],
        "source_ticket": "AGENTS-136",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# check_contract — valid contract
# ---------------------------------------------------------------------------

def test_valid_contract_passes():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract())
    assert errors == []


def test_valid_contract_with_high_risk_and_human_gate():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(risk="high", human_gate_required=True))
    assert errors == []


def test_valid_contract_critical_risk():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(risk="critical"))
    assert errors == []


def test_valid_contract_ignores_extra_fields():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    contract["role"] = "worker"
    contract["unknown_extra"] = "whatever"
    errors = check_contract(contract)
    assert errors == []


# ---------------------------------------------------------------------------
# check_contract — missing fields
# ---------------------------------------------------------------------------

def test_missing_intended_behavior():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["intended_behavior"]
    errors = check_contract(contract)
    assert any("intended_behavior" in e for e in errors)


def test_missing_source_ticket():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["source_ticket"]
    errors = check_contract(contract)
    assert any("source_ticket" in e for e in errors)


def test_missing_non_goals():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["non_goals"]
    errors = check_contract(contract)
    assert any("non_goals" in e for e in errors)


def test_missing_assertions():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["assertions"]
    errors = check_contract(contract)
    assert any("assertions" in e for e in errors)


def test_missing_commands():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["commands"]
    errors = check_contract(contract)
    assert any("commands" in e for e in errors)


def test_missing_bounce_conditions():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["bounce_conditions"]
    errors = check_contract(contract)
    assert any("bounce_conditions" in e for e in errors)


def test_missing_behavior_check_required():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["behavior_check_required"]
    errors = check_contract(contract)
    assert any("behavior_check_required" in e for e in errors)


def test_missing_risk():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["risk"]
    errors = check_contract(contract)
    assert any("risk" in e for e in errors)


def test_missing_human_gate_required():
    from hermes_cli.team_os.contracts import check_contract

    contract = _valid_contract()
    del contract["human_gate_required"]
    errors = check_contract(contract)
    assert any("human_gate_required" in e for e in errors)


def test_all_fields_missing_produces_nine_or_more_errors():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract({})
    assert len(errors) >= 9


# ---------------------------------------------------------------------------
# check_contract — empty strings
# ---------------------------------------------------------------------------

def test_empty_intended_behavior():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(intended_behavior=""))
    assert any("intended_behavior" in e for e in errors)


def test_whitespace_only_intended_behavior():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(intended_behavior="   "))
    assert any("intended_behavior" in e for e in errors)


def test_empty_source_ticket():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(source_ticket=""))
    assert any("source_ticket" in e for e in errors)


# ---------------------------------------------------------------------------
# check_contract — empty lists
# ---------------------------------------------------------------------------

def test_empty_non_goals_list():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(non_goals=[]))
    assert any("non_goals" in e for e in errors)


def test_empty_assertions_list():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(assertions=[]))
    assert any("assertions" in e for e in errors)


def test_empty_commands_list():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(commands=[]))
    assert any("commands" in e for e in errors)


def test_empty_bounce_conditions_list():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(bounce_conditions=[]))
    assert any("bounce_conditions" in e for e in errors)


def test_list_with_empty_string_item():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(assertions=["valid entry", ""]))
    assert any("assertions" in e for e in errors)


def test_list_with_whitespace_only_item():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(commands=["pytest", "   "]))
    assert any("commands" in e for e in errors)


# ---------------------------------------------------------------------------
# check_contract — invalid risk values
# ---------------------------------------------------------------------------

def test_invalid_risk_unknown_string():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(risk="ultra-dangerous"))
    assert any("risk" in e for e in errors)


def test_invalid_risk_none():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(risk=None))
    assert any("risk" in e for e in errors)


def test_invalid_risk_integer():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(risk=3))
    assert any("risk" in e for e in errors)


def test_all_valid_risk_levels_accepted():
    from hermes_cli.team_os.contracts import check_contract, VALID_RISK_LEVELS

    for level in VALID_RISK_LEVELS:
        errors = check_contract(_valid_contract(risk=level))
        assert errors == [], f"Expected no errors for risk={level!r}, got {errors}"


# ---------------------------------------------------------------------------
# check_contract — invalid human_gate_required types
# ---------------------------------------------------------------------------

def test_human_gate_required_as_string_yes():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(human_gate_required="yes"))
    assert any("human_gate_required" in e for e in errors)


def test_human_gate_required_as_string_always():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(human_gate_required="always"))
    assert any("human_gate_required" in e for e in errors)


def test_human_gate_required_as_integer():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(human_gate_required=1))
    assert any("human_gate_required" in e for e in errors)


def test_human_gate_required_as_none():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(human_gate_required=None))
    assert any("human_gate_required" in e for e in errors)


# ---------------------------------------------------------------------------
# check_contract — invalid behavior_check_required types
# ---------------------------------------------------------------------------

def test_behavior_check_required_as_string():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(behavior_check_required="true"))
    assert any("behavior_check_required" in e for e in errors)


def test_behavior_check_required_as_integer():
    from hermes_cli.team_os.contracts import check_contract

    errors = check_contract(_valid_contract(behavior_check_required=1))
    assert any("behavior_check_required" in e for e in errors)


# ---------------------------------------------------------------------------
# Template definitions
# ---------------------------------------------------------------------------

def test_planner_template_is_valid_contract():
    from hermes_cli.team_os.contracts import check_contract, PLANNER_TEMPLATE

    errors = check_contract(PLANNER_TEMPLATE)
    assert errors == []


def test_worker_template_is_valid_contract():
    from hermes_cli.team_os.contracts import check_contract, WORKER_TEMPLATE

    errors = check_contract(WORKER_TEMPLATE)
    assert errors == []


def test_validator_template_is_valid_contract():
    from hermes_cli.team_os.contracts import check_contract, VALIDATOR_TEMPLATE

    errors = check_contract(VALIDATOR_TEMPLATE)
    assert errors == []


def test_planner_template_has_role_field():
    from hermes_cli.team_os.contracts import PLANNER_TEMPLATE

    assert PLANNER_TEMPLATE["role"] == "planner"


def test_worker_template_has_role_field():
    from hermes_cli.team_os.contracts import WORKER_TEMPLATE

    assert WORKER_TEMPLATE["role"] == "worker"


def test_validator_template_has_role_field():
    from hermes_cli.team_os.contracts import VALIDATOR_TEMPLATE

    assert VALIDATOR_TEMPLATE["role"] == "validator"


def test_validator_template_requires_human_gate():
    from hermes_cli.team_os.contracts import VALIDATOR_TEMPLATE

    assert VALIDATOR_TEMPLATE["human_gate_required"] is True


def test_planner_and_worker_templates_do_not_require_human_gate():
    from hermes_cli.team_os.contracts import PLANNER_TEMPLATE, WORKER_TEMPLATE

    assert PLANNER_TEMPLATE["human_gate_required"] is False
    assert WORKER_TEMPLATE["human_gate_required"] is False


def test_render_template_returns_copy():
    from hermes_cli.team_os.contracts import render_template, PLANNER_TEMPLATE

    result = render_template("planner")
    assert result == PLANNER_TEMPLATE
    result["role"] = "mutated"
    assert PLANNER_TEMPLATE["role"] == "planner"


def test_render_template_returns_deep_copy_for_list_fields():
    from hermes_cli.team_os.contracts import render_template, PLANNER_TEMPLATE

    result = render_template("planner")
    result["non_goals"].append("mutated by caller")
    result["assertions"][0] = "mutated assertion"

    assert "mutated by caller" not in PLANNER_TEMPLATE["non_goals"]
    assert PLANNER_TEMPLATE["assertions"][0] == "All subtasks reference a source_ticket"


def test_render_template_raises_for_unknown_role():
    import pytest
    from hermes_cli.team_os.contracts import render_template

    with pytest.raises(ValueError, match="unknown role"):
        render_template("orchestrator")


def test_templates_dict_has_all_three_roles():
    from hermes_cli.team_os.contracts import TEMPLATES

    assert set(TEMPLATES.keys()) == {"planner", "worker", "validator"}


# ---------------------------------------------------------------------------
# CLI: render-template
# ---------------------------------------------------------------------------

def test_cli_render_template_none_role_is_error_not_silent_default(capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    # Argparse makes role required, so None can only arrive programmatically.
    # The dead `or "planner"` fallback was removed; None must now surface as rc=1.
    args = argparse.Namespace(
        team_os_command="render-template",
        role=None,
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "unknown role None" in captured.err


def test_cli_render_template_missing_role_is_error_not_uncaught_exception(capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    args = argparse.Namespace(
        team_os_command="render-template",
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "unknown role None" in captured.err


def test_cli_render_template_planner(capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    args = argparse.Namespace(
        team_os_command="render-template",
        role="planner",
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["role"] == "planner"
    assert "intended_behavior" in data
    assert "commands" in data


def test_cli_render_template_worker(capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    args = argparse.Namespace(
        team_os_command="render-template",
        role="worker",
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["role"] == "worker"


def test_cli_render_template_validator(capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    args = argparse.Namespace(
        team_os_command="render-template",
        role="validator",
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["role"] == "validator"
    assert data["human_gate_required"] is True


def test_cli_render_template_writes_output_file(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    output_file = tmp_path / "planner.json"
    args = argparse.Namespace(
        team_os_command="render-template",
        role="planner",
        output=str(output_file),
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output_file.read_text(encoding="utf-8"))
    assert data["role"] == "planner"
    captured = capsys.readouterr()
    assert str(output_file) in captured.out


# ---------------------------------------------------------------------------
# CLI: check-contract
# ---------------------------------------------------------------------------

def test_cli_check_contract_valid(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_valid_contract()), encoding="utf-8")

    args = argparse.Namespace(
        team_os_command="check-contract",
        contract_file=str(contract_path),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["valid"] is True
    assert data["errors"] == []


def test_cli_check_contract_invalid_returns_rc1(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    bad_contract = _valid_contract(intended_behavior="", risk="bad-value")
    contract_path = tmp_path / "bad.json"
    contract_path.write_text(json.dumps(bad_contract), encoding="utf-8")

    args = argparse.Namespace(
        team_os_command="check-contract",
        contract_file=str(contract_path),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["valid"] is False
    assert len(data["errors"]) >= 2


def test_cli_check_contract_missing_file_returns_rc1(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    args = argparse.Namespace(
        team_os_command="check-contract",
        contract_file=str(tmp_path / "nonexistent.json"),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 1


def test_cli_check_contract_writes_output_file(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_valid_contract()), encoding="utf-8")
    output_path = tmp_path / "result.json"

    args = argparse.Namespace(
        team_os_command="check-contract",
        contract_file=str(contract_path),
        output=str(output_path),
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["valid"] is True
    captured = capsys.readouterr()
    assert str(output_path) in captured.out


def test_cli_check_contract_reports_all_errors(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    contract_path = tmp_path / "empty.json"
    contract_path.write_text("{}", encoding="utf-8")

    args = argparse.Namespace(
        team_os_command="check-contract",
        contract_file=str(contract_path),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["errors"]) >= 9
