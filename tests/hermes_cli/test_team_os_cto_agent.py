"""Stage B: the real CTO contract-scoper (hermes_cli/team_os/cto_agent.py).

Stub reviewers (no tokens). Covers: valid-contract assembly, fail-closed
human-gate on gated tickets, template fallback on error/invalid, and that the
output always passes contracts.check_contract().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.team_os.cto_agent import cto_contract, build_cto_prompt, _parse_contract  # noqa: E402
from hermes_cli.team_os.contracts import check_contract  # noqa: E402

TICKET = {"identifier": "AGENTS-50", "title": "Add retry to fetch_x", "body": "wrap fetch_x in 3-retry backoff", "project": "Hermes System"}
CORTEX_SAFE = {"gated": False, "decision": "safe", "system": "HERMES", "severity": "low", "root_cause": "no retry", "grounding_summary": "add backoff to fetch_x"}
CORTEX_GATED = {"gated": True, "decision": "gated", "system": "HERMES", "severity": "high", "root_cause": "touches creds", "grounding_summary": "credential rotation"}


def _good_contract_json(**over):
    base = {
        "intended_behavior": "Wrap fetch_x in a 3-retry exponential backoff",
        "files_to_touch": ["hermes_cli/net.py", "tests/test_net.py"],
        "non_goals": ["Do not change the public signature"],
        "assertions": ["retry test passes", "ruff clean"],
        "commands": ["pytest tests/test_net.py -q"],
        "bounce_conditions": ["test fails", "kill-switch active"],
        "risk": "low", "behavior_check_required": True,
        "human_gate_required": False, "scope_summary": "small net change",
    }
    base.update(over)
    return json.dumps(base)


class TestRealContract:
    def test_produces_valid_contract(self):
        c = cto_contract(TICKET, CORTEX_SAFE, reviewer=lambda p: _good_contract_json(), enabled=True)
        assert c["contract_source"] == "cto-agent"
        assert check_contract(c) == []  # passes the real validator
        assert "hermes_cli/net.py" in c["files_to_touch"]
        assert c["source_ticket"] == "AGENTS-50"

    def test_gated_forces_human_gate_even_if_model_says_false(self):
        # model returns human_gate_required False, but Cortex gated it → fail closed
        c = cto_contract(TICKET, CORTEX_GATED, reviewer=lambda p: _good_contract_json(human_gate_required=False), enabled=True)
        assert c["human_gate_required"] is True
        assert c["contract_source"] == "cto-agent"

    def test_carries_risk_through(self):
        c = cto_contract(TICKET, CORTEX_SAFE, reviewer=lambda p: _good_contract_json(risk="high"), enabled=True)
        assert c["risk"] == "high"


class TestFailSafe:
    def test_disabled_uses_template(self):
        c = cto_contract(TICKET, CORTEX_SAFE, reviewer=lambda p: _good_contract_json(), enabled=False)
        assert c["contract_source"] == "template-fallback"
        assert check_contract(c) == []

    def test_reviewer_error_falls_back(self):
        def boom(p): raise RuntimeError("rail down")
        c = cto_contract(TICKET, CORTEX_SAFE, reviewer=boom, enabled=True)
        assert c["contract_source"] == "template-fallback" and "rail down" in c["fallback_reason"]
        assert check_contract(c) == []

    def test_invalid_contract_falls_back(self):
        # model omits required assertions → invalid → template fallback
        c = cto_contract(TICKET, CORTEX_SAFE, enabled=True,
                         reviewer=lambda p: json.dumps({"intended_behavior": "x", "risk": "low"}))
        assert c["contract_source"] == "template-fallback"
        assert check_contract(c) == []

    def test_fallback_on_gated_still_human_gated(self):
        c = cto_contract(TICKET, CORTEX_GATED, reviewer=lambda p: "no json", enabled=True)
        assert c["contract_source"] == "template-fallback" and c["human_gate_required"] is True

    def test_bad_risk_value_falls_back(self):
        c = cto_contract(TICKET, CORTEX_SAFE, enabled=True, reviewer=lambda p: _good_contract_json(risk="spicy"))
        assert c["contract_source"] == "template-fallback"  # check_contract rejects bad risk


class TestPrompt:
    def test_prompt_includes_ticket_and_cortex_grounding(self):
        p = build_cto_prompt(TICKET, CORTEX_GATED)
        assert "AGENTS-50" in p and "credential rotation" in p and "files_to_touch" in p
        assert "Use NO tools" in p


class TestDefinitionOfDone:
    """§9 verifiable-goal: the contract must carry a concrete, runnable done-check."""

    def test_carries_definition_of_done_through(self):
        c = cto_contract(TICKET, CORTEX_SAFE, enabled=True,
                         reviewer=lambda p: _good_contract_json(
                             definition_of_done="pytest tests/test_net.py::test_retry exits 0"))
        assert c["contract_source"] == "cto-agent"
        assert c["definition_of_done"] == "pytest tests/test_net.py::test_retry exits 0"
        assert check_contract(c) == []  # optional field doesn't break validation

    def test_prompt_demands_a_runnable_done_check(self):
        p = build_cto_prompt(TICKET, CORTEX_SAFE)
        assert "definition_of_done" in p
        assert "run" in p.lower() and "placeholder" in p.lower()  # the verifiable-goal rule

    def test_missing_commands_signals_incomplete_not_a_fake_command(self):
        # model returns a valid contract but NO real proof command → we must NOT
        # paper it with a fake command; signal incompleteness so the Validator bounces.
        c = cto_contract(TICKET, CORTEX_SAFE, enabled=True,
                         reviewer=lambda p: _good_contract_json(commands=[]))
        assert c["contract_source"] == "cto-agent"
        assert any("no proof command" in str(x).lower() for x in c["commands"])
        assert not any("loop-runner" in str(x) for x in c["commands"])  # no fake placeholder
