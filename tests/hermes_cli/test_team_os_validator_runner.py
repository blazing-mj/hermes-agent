"""Automated Team OS Validator runner tests for AGENTS-174."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _contract() -> dict:
    return {
        "source_ticket": "AGENTS-174-EASY",
        "intended_behavior": "Add a tiny Validator-runner fixture and prove BOUNCE then PASS.",
        "non_goals": ["Do not auto-close Linear", "Do not dispatch workers"],
        "assertions": [
            "Intent is preserved",
            "Scope is limited to the fixture",
            "Acceptance criterion is met",
            "Implementation matches the requested fixture behavior",
            "Proof command output is present",
        ],
        "commands": ["python3.13 -m pytest tests/hermes_cli/test_team_os_validator_runner.py -q"],
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": ["Proof command output is missing", "Implementation changes unrelated files"],
    }


def _handoff(*, flawed: bool) -> dict:
    return {
        "source_ticket": "AGENTS-174-EASY",
        "intent": "Add a tiny Validator-runner fixture and prove BOUNCE then PASS.",
        "scope": ["tests/hermes_cli/test_team_os_validator_runner.py"],
        "acceptance": ["Validator-runner returns BOUNCE for planted flaw and PASS after proof is fixed"],
        "implementation": "Fixture added; no dispatch or Linear status mutation performed.",
        "proof": [] if flawed else ["python3.13 -m pytest tests/hermes_cli/test_team_os_validator_runner.py -q => passed"],
    }


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_validator_prompt_enforces_five_step_order_and_human_gate(tmp_path):
    from hermes_cli.team_os.validator_runner import build_validator_prompt

    prompt = build_validator_prompt(contract=_contract(), handoff=_handoff(flawed=False))

    expected = [
        "1. intent",
        "2. scope",
        "3. acceptance",
        "4. implementation",
        "5. proof",
    ]
    positions = [prompt.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "Human gate stays ON" in prompt
    assert "Do not mark Linear Done" in prompt
    assert "VERDICT: PASS" in prompt
    assert "VERDICT: BOUNCE" in prompt


def test_validator_runner_bounces_planted_flaw_then_passes_fixed_handoff(tmp_path):
    from hermes_cli.team_os.validator_runner import run_validator

    contract_path = _write_json(tmp_path / "contract.json", _contract())
    bad_path = _write_json(tmp_path / "bad-handoff.json", _handoff(flawed=True))
    good_path = _write_json(tmp_path / "good-handoff.json", _handoff(flawed=False))
    state_path = tmp_path / "bounce-state.json"

    def fake_cold_reviewer(prompt: str) -> str:
        return "VERDICT: BOUNCE\nmissing proof" if '"proof": []' in prompt else "VERDICT: PASS\nproof present"

    bad = run_validator(
        contract_path=contract_path,
        handoff_path=bad_path,
        state_path=state_path,
        reviewer=fake_cold_reviewer,
    )
    good = run_validator(
        contract_path=contract_path,
        handoff_path=good_path,
        state_path=state_path,
        reviewer=fake_cold_reviewer,
    )

    assert bad["verdict"] == "BOUNCE"
    assert bad["bounce_count"] == 1
    assert bad["escalate_mj"] is False
    assert bad["human_gate_required"] is True
    assert bad["auto_done_allowed"] is False
    assert bad["loop_feed_allowed"] is False
    assert good["verdict"] == "PASS"
    assert good["bounce_count"] == 0
    assert good["human_gate_required"] is True
    assert good["auto_done_allowed"] is False
    assert good["loop_feed_allowed"] is False


def test_validator_runner_tripwire_escalates_to_mj_at_two_bounces(tmp_path):
    from hermes_cli.team_os.validator_runner import run_validator

    contract_path = _write_json(tmp_path / "contract.json", _contract())
    bad_path = _write_json(tmp_path / "bad-handoff.json", _handoff(flawed=True))
    state_path = tmp_path / "bounce-state.json"

    result1 = run_validator(
        contract_path=contract_path,
        handoff_path=bad_path,
        state_path=state_path,
        reviewer=lambda prompt: "VERDICT: BOUNCE\nmissing proof",
    )
    result2 = run_validator(
        contract_path=contract_path,
        handoff_path=bad_path,
        state_path=state_path,
        reviewer=lambda prompt: "VERDICT: BOUNCE\nmissing proof again",
    )

    assert result1["bounce_count"] == 1
    assert result1["escalate_mj"] is False
    assert result2["bounce_count"] == 2
    assert result2["escalate_mj"] is True
    assert "MJ" in result2["tripwire"]


def test_validator_runner_parses_claude_max_json_result_wrapper(tmp_path):
    from hermes_cli.team_os.validator_runner import run_validator

    contract_path = _write_json(tmp_path / "contract.json", _contract())
    good_path = _write_json(tmp_path / "good-handoff.json", _handoff(flawed=False))
    state_path = tmp_path / "bounce-state.json"

    result = run_validator(
        contract_path=contract_path,
        handoff_path=good_path,
        state_path=state_path,
        reviewer=lambda prompt: json.dumps({"type": "result", "result": "VERDICT: PASS\nstep_summary: ok"}),
    )

    assert result["verdict"] == "PASS"
    assert result["bounce_count"] == 0


def test_validator_runner_cli_returns_nonzero_for_bounce_and_zero_for_pass(tmp_path):
    import subprocess
    import sys

    contract_path = _write_json(tmp_path / "contract.json", _contract())
    bad_path = _write_json(tmp_path / "bad-handoff.json", _handoff(flawed=True))
    good_path = _write_json(tmp_path / "good-handoff.json", _handoff(flawed=False))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(
        "import os, pathlib\n"
        "prompt = pathlib.Path(os.environ['TEAM_OS_VALIDATOR_PROMPT']).read_text()\n"
        "print('VERDICT: BOUNCE' if '\"proof\": []' in prompt else 'VERDICT: PASS')\n",
        encoding="utf-8",
    )

    cli = (
        "import argparse, sys; "
        "from hermes_cli.team_os.cli import register_cli; "
        "p=argparse.ArgumentParser(); register_cli(p); "
        "a=p.parse_args(sys.argv[1:]); "
        "raise SystemExit(a.func(a))"
    )
    base = [
        sys.executable,
        "-c",
        cli,
        "validate-handoff",
        "--contract",
        str(contract_path),
        "--state",
        str(tmp_path / "state.json"),
        "--review-cmd",
        sys.executable,
        str(reviewer),
    ]
    bad = subprocess.run(base + ["--handoff", str(bad_path)], cwd=Path.cwd(), text=True, capture_output=True, timeout=30)
    good = subprocess.run(base + ["--handoff", str(good_path)], cwd=Path.cwd(), text=True, capture_output=True, timeout=30)

    assert bad.returncode == 1
    assert '"verdict": "BOUNCE"' in bad.stdout
    assert good.returncode == 0
    assert '"verdict": "PASS"' in good.stdout


def test_bounce_loop_feeds_cruel_validator_critique_to_worker_fix_then_passes(tmp_path):
    from hermes_cli.team_os.validator_runner import run_bounce_loop

    calls: list[dict] = []

    def fake_cold_reviewer(prompt: str) -> str:
        return "VERDICT: BOUNCE\nstep_summary: proof missing" if '"proof": []' in prompt else "VERDICT: PASS\nstep_summary: proof fixed"

    def fake_worker_fixer(contract: dict, handoff: dict, validator_result: dict, attempt: int) -> dict:
        calls.append({"attempt": attempt, "verdict": validator_result["verdict"], "review_text": validator_result["review_text"]})
        fixed = dict(handoff)
        fixed["proof"] = ["python3.13 -m pytest tests/hermes_cli/test_team_os_validator_runner.py -q => passed"]
        return fixed

    result = run_bounce_loop(
        contract=_contract(),
        initial_handoff=_handoff(flawed=True),
        state_path=tmp_path / "bounce-state.json",
        reviewer=fake_cold_reviewer,
        worker_fixer=fake_worker_fixer,
        max_bounces=3,
    )

    assert result["status"] == "passed"
    assert [r["verdict"] for r in result["validator_results"]] == ["BOUNCE", "PASS"]
    assert result["attempts"] == 2
    assert calls == [{"attempt": 1, "verdict": "BOUNCE", "review_text": "VERDICT: BOUNCE\nstep_summary: proof missing"}]
    assert result["human_gate_required"] is True
    assert result["auto_done_allowed"] is False
    assert result["loop_feed_allowed"] is False


def test_bounce_loop_stops_after_three_bounces_and_escalates_to_mj(tmp_path):
    from hermes_cli.team_os.validator_runner import run_bounce_loop

    fixer_attempts: list[int] = []

    def still_bad(contract: dict, handoff: dict, validator_result: dict, attempt: int) -> dict:
        fixer_attempts.append(attempt)
        return dict(handoff)

    result = run_bounce_loop(
        contract=_contract(),
        initial_handoff=_handoff(flawed=True),
        state_path=tmp_path / "bounce-state.json",
        reviewer=lambda prompt: "VERDICT: BOUNCE\nstep_summary: still bad",
        worker_fixer=still_bad,
        max_bounces=3,
    )

    assert result["status"] == "max_bounces_exceeded"
    assert result["attempts"] == 3
    assert [r["verdict"] for r in result["validator_results"]] == ["BOUNCE", "BOUNCE", "BOUNCE"]
    assert fixer_attempts == [1, 2]
    assert result["escalate_mj"] is True
    assert result["human_gate_required"] is True
    assert result["auto_done_allowed"] is False


def test_validator_prompt_enforces_definition_of_done():
    from hermes_cli.team_os.validator_runner import build_validator_prompt
    contract = {
        "source_ticket": "AGENTS-1", "intended_behavior": "x", "non_goals": [],
        "assertions": ["a"], "commands": ["pytest -q"], "bounce_conditions": ["b"],
        "behavior_check_required": True, "human_gate_required": True, "risk": "low",
        "definition_of_done": "pytest tests/test_x.py::test_y exits 0",
    }
    prompt = build_validator_prompt(contract=contract, handoff={"schema": "team_os.worker_handoff.v1"})
    assert "definition_of_done" in prompt
    assert "not demonstrated" in prompt  # declared-but-unproven done-check = BOUNCE
