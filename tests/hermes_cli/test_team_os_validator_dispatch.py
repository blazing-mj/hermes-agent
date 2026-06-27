"""Stage D: the Validator connector (hermes_cli/team_os/validator_dispatch.py).

Stub reviewers (no tokens). Covers: off-by-default, PASS, BOUNCE + bounce
counting + escalation, and fail-closed (errors → BOUNCE, never PASS).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path("/Users/alfred/.hermes/hermes-agent")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.team_os.validator_dispatch import dispatch_validator  # noqa: E402

CONTRACT = {
    "source_ticket": "AGENTS-70", "role": "worker",
    "intended_behavior": "do x", "non_goals": ["n"], "assertions": ["a"],
    "commands": ["c"], "bounce_conditions": ["b"], "risk": "low",
    "behavior_check_required": True, "human_gate_required": False,
}
HANDOFF = {"schema": "team_os.worker_handoff.v1", "source_ticket": "AGENTS-70",
           "changed_files": ["x.py"], "proof_results": [{"cmd": "pytest", "exit": 0}],
           "human_gate_required": True}


def test_off_by_default_bounces():
    out = dispatch_validator(CONTRACT, HANDOFF, reviewer=lambda p: "VERDICT: PASS", enabled=False)
    assert out["validated"] is False and out["verdict"] == "BOUNCE"


def test_pass_verdict(tmp_path):
    out = dispatch_validator(CONTRACT, HANDOFF, reviewer=lambda p: "VERDICT: PASS",
                             state_path=tmp_path / "b.json", enabled=True)
    assert out["validated"] is True and out["verdict"] == "PASS"
    assert out["bounce_count"] == 0 and out["escalate_mj"] is False


def test_bounce_counts_and_escalates(tmp_path):
    sp = tmp_path / "b.json"
    o1 = dispatch_validator(CONTRACT, HANDOFF, reviewer=lambda p: "VERDICT: BOUNCE", state_path=sp, enabled=True)
    o2 = dispatch_validator(CONTRACT, HANDOFF, reviewer=lambda p: "VERDICT: BOUNCE", state_path=sp, enabled=True)
    assert o1["verdict"] == "BOUNCE" and o1["bounce_count"] == 1 and o1["escalate_mj"] is False
    assert o2["bounce_count"] == 2 and o2["escalate_mj"] is True  # MJ escalation at 2


def test_reviewer_error_fails_closed(tmp_path):
    def boom(p): raise RuntimeError("rail down")
    out = dispatch_validator(CONTRACT, HANDOFF, reviewer=boom, state_path=tmp_path / "b.json", enabled=True)
    # validator_runner catches reviewer errors internally → BOUNCE; either way never PASS
    assert out["verdict"] == "BOUNCE"


def test_invalid_contract_bounces(tmp_path):
    bad = {"source_ticket": "AGENTS-70"}  # missing required fields
    out = dispatch_validator(bad, HANDOFF, reviewer=lambda p: "VERDICT: PASS",
                             state_path=tmp_path / "b.json", enabled=True)
    assert out["verdict"] == "BOUNCE"  # invalid contract can never PASS


def test_garbage_verdict_fails_closed(tmp_path):
    out = dispatch_validator(CONTRACT, HANDOFF, reviewer=lambda p: "the model rambled",
                             state_path=tmp_path / "b.json", enabled=True)
    assert out["verdict"] == "BOUNCE"


def test_default_verifier_is_codex_cross_model(monkeypatch, tmp_path):
    """With no reviewer injected, the verifier defaults to codex (cross-model
    from the opus worker), not the engine's same-model opus default."""
    import hermes_cli.team_os.validator_dispatch as vd
    used = {}

    def _fake_codex(p, **k):
        used["called"] = True
        return "VERDICT: PASS"

    monkeypatch.setattr(vd, "codex_reviewer", _fake_codex)
    out = vd.dispatch_validator(CONTRACT, HANDOFF, state_path=tmp_path / "b.json", enabled=True)
    assert used.get("called") is True and out["verdict"] == "PASS"


def test_codex_reviewer_empty_on_error_fails_closed(monkeypatch):
    import hermes_cli.team_os.validator_dispatch as vd
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("codex down")))
    assert vd.codex_reviewer("x") == ""  # error → empty → validator bounces
