"""Phase 5 task router tests for hermes team-os.

Strict TDD: these are the failing specs written before the router exists.
Routing rules under test:
    - Heavy implementation/review tasks must prefer claude-max and refuse
      automatic Codex fallback even if Codex quota looks generous.
    - Codex may only win for host/direct-chat shaped tasks AND only when a
      probe confirms availability.
    - Quota confidence below "high" or "medium" blocks the dispatcher.
    - Probe failure flips Codex availability to blocked.
    - There is NO automatic API fallback: when no subscription is safe,
      the decision is "none" and requires human dispatch.
"""

from __future__ import annotations

import json

import pytest


def _hints(**overrides):
    from hermes_cli.team_os.router import TaskHints

    defaults = dict(
        task_id="AGENTS-72",
        labels=(),
        task_type="unknown",
        quota_confidence_codex="unknown",
        quota_confidence_claude_max="unknown",
    )
    defaults.update(overrides)
    return TaskHints(**defaults)


_ROUTING_CASES = [
    dict(
        case_id="heavy-code-prefers-claude-max",
        hints=dict(task_type="code", quota_confidence_claude_max="high"),
        codex_probe=lambda: True,
        expected_dispatcher="claude-max",
        reason_contains="implementation",
    ),
    dict(
        case_id="review-label-prefers-claude-max",
        hints=dict(labels=("type:review",), quota_confidence_claude_max="high"),
        codex_probe=lambda: True,
        expected_dispatcher="claude-max",
        reason_contains="review",
    ),
    dict(
        case_id="host-task-allows-codex-when-probe-passes",
        hints=dict(task_type="host", quota_confidence_codex="high"),
        codex_probe=lambda: True,
        expected_dispatcher="codex",
        reason_contains="host",
    ),
    dict(
        case_id="direct-chat-label-allows-codex",
        hints=dict(labels=("type:direct-chat",), quota_confidence_codex="high"),
        codex_probe=lambda: True,
        expected_dispatcher="codex",
        reason_contains="direct-chat",
    ),
    dict(
        case_id="code-task-refuses-codex-even-with-high-codex-quota",
        hints=dict(task_type="code", quota_confidence_codex="high"),
        codex_probe=lambda: True,
        expected_dispatcher="none",
        reason_contains="no automatic",
    ),
    dict(
        case_id="both-quotas-unknown-no-dispatch",
        hints=dict(task_type="code"),
        codex_probe=lambda: True,
        expected_dispatcher="none",
        reason_contains="quota confidence",
    ),
    dict(
        case_id="host-task-codex-unavailable-no-dispatch",
        hints=dict(task_type="host", quota_confidence_codex="high"),
        codex_probe=lambda: False,
        expected_dispatcher="none",
        reason_contains="codex",
    ),
    dict(
        case_id="host-task-claude-max-fallback-only-if-quota-high",
        hints=dict(
            task_type="host",
            quota_confidence_codex="unknown",
            quota_confidence_claude_max="high",
        ),
        codex_probe=lambda: True,
        expected_dispatcher="claude-max",
        reason_contains="claude-max",
    ),
    dict(
        case_id="low-quota-blocks-claude-max",
        hints=dict(task_type="code", quota_confidence_claude_max="low"),
        codex_probe=lambda: True,
        expected_dispatcher="none",
        reason_contains="quota confidence",
    ),
]


@pytest.mark.parametrize("case", _ROUTING_CASES, ids=[c["case_id"] for c in _ROUTING_CASES])
def test_router_table_driven_routing_cases(case):
    from hermes_cli.team_os.router import route_task

    hints = _hints(**case["hints"])
    decision = route_task(hints, codex_probe=case["codex_probe"])

    assert decision.dispatcher == case["expected_dispatcher"], (
        f"{case['case_id']}: expected {case['expected_dispatcher']} got {decision.dispatcher}"
    )
    assert case["reason_contains"].lower() in decision.reason.lower(), (
        f"{case['case_id']}: reason {decision.reason!r} missing {case['reason_contains']!r}"
    )
    assert decision.dry_run is True
    assert decision.task_id == hints.task_id


def test_router_probe_failure_flips_codex_availability_to_blocked():
    from hermes_cli.team_os.router import route_task

    hints = _hints(
        task_type="host",
        quota_confidence_codex="high",
        quota_confidence_claude_max="unknown",
    )

    def probe_raises():
        raise RuntimeError("codex probe failed: not installed")

    decision = route_task(hints, codex_probe=probe_raises)

    assert decision.dispatcher == "none"
    assert decision.considered["codex"].startswith("blocked")
    assert "probe" in decision.considered["codex"].lower()
    assert decision.requires_human_dispatch is True


def test_router_default_probe_treats_codex_as_unconfirmed():
    """Codex usage is not confirmed; default availability must be False."""
    from hermes_cli.team_os.router import route_task

    hints = _hints(task_type="host", quota_confidence_codex="high")

    # No probe injected — must default to unconfirmed/unavailable.
    decision = route_task(hints)

    assert decision.dispatcher == "none"
    assert decision.considered["codex"].startswith("blocked")


def test_router_explicitly_refuses_automatic_api_fallback_for_heavy_code():
    from hermes_cli.team_os.router import route_task

    hints = _hints(
        labels=("type:code",),
        task_type="code",
        quota_confidence_codex="high",
        quota_confidence_claude_max="exhausted",
    )

    decision = route_task(hints, codex_probe=lambda: True)

    assert decision.dispatcher == "none"
    assert decision.requires_human_dispatch is True
    explanation = (decision.explanation + " " + decision.reason).lower()
    assert "no automatic api fallback" in explanation


def test_router_decision_to_dict_serializes_for_logging():
    from hermes_cli.team_os.router import route_task

    hints = _hints(
        labels=("type:code",),
        task_type="code",
        quota_confidence_codex="unknown",
        quota_confidence_claude_max="high",
    )

    decision = route_task(hints, codex_probe=lambda: True)
    data = decision.to_dict()

    assert data["task_id"] == "AGENTS-72"
    assert data["dispatcher"] == "claude-max"
    assert data["dry_run"] is True
    assert "considered" in data and "codex" in data["considered"]
    assert "explanation" in data and data["explanation"]


def test_router_cli_route_writes_decision_json(tmp_path):
    from argparse import Namespace

    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "route.json"
    args = Namespace(
        team_os_command="route",
        task_id="AGENTS-72",
        label=["type:code"],
        task_type="code",
        quota_confidence_codex="unknown",
        quota_confidence_claude_max="high",
        codex_probe="unavailable",
        output=str(output),
    )

    rc = cmd_team_os(args)

    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_id"] == "AGENTS-72"
    assert data["dispatcher"] == "claude-max"
    assert data["dry_run"] is True
    assert data["considered"]["codex"].startswith("blocked")
