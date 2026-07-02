"""Stage A: the real Cortex triage agent (hermes_cli/team_os/cortex_agent.py).

Tests use stub reviewers (deterministic) so no model tokens are spent; a real
LLM smoke is run separately. Covers: audit/classify parsing, questions,
safe-vs-gated decision, fail-safe fallback, and the keyword safety cross-check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.team_os.cortex_agent import (  # noqa: E402
    build_cortex_prompt,
    cortex_audit,
    _parse_verdict,
    _flag_on,
)


def _verdict(**over):
    base = {
        "is_real": True, "worth_doing": True, "already_done_or_duplicate": False,
        "system": "HERMES", "severity": "low", "root_cause": "x", "confidence": 0.9,
        "decision": "safe", "gated_reason": "", "questions": [],
        "grounding_summary": "g", "recommended_route": "claude-max", "reason": "ok",
    }
    base.update(over)
    return json.dumps(base)


SAFE = {"identifier": "AGENTS-1", "title": "Add a docstring", "body": "reversible docs change"}
GATED = {"identifier": "AGENTS-2", "title": "Rotate API credentials", "body": "touch live credentials"}
VAGUE = {"identifier": "AGENTS-3", "title": "fix it", "body": "tbd"}


class TestParsing:
    def test_plain_json(self):
        v = _parse_verdict(_verdict(decision="gated"))
        assert v and v["decision"] == "gated"

    def test_fenced_json(self):
        v = _parse_verdict("```json\n" + _verdict() + "\n```")
        assert v and v["decision"] == "safe"

    def test_result_wrapper(self):
        v = _parse_verdict(json.dumps({"result": _verdict(decision="needs-question")}))
        assert v and v["decision"] == "needs-question"

    def test_garbage_returns_none(self):
        assert _parse_verdict("the model rambled with no json") is None

    def test_invalid_decision_rejected(self):
        assert _parse_verdict(_verdict(decision="maybe")) is None


class TestDecision:
    def test_safe_is_not_gated(self):
        out = cortex_audit(SAFE, keyword_gated=False, reviewer=lambda p: _verdict(decision="safe"), enabled=True)
        assert out["gated"] is False and out["source"] == "cortex-agent"

    def test_gated_is_gated(self):
        out = cortex_audit(GATED, keyword_gated=True, reviewer=lambda p: _verdict(decision="gated", gated_reason="credentials"), enabled=True)
        assert out["gated"] is True and "credentials" in out["gated_reason"]

    def test_needs_question_stops_autoflow(self):
        out = cortex_audit(VAGUE, keyword_gated=False, enabled=True,
                           reviewer=lambda p: _verdict(decision="needs-question", questions=["What system?", "Reversible?"]))
        assert out["gated"] is True  # needs-question must not auto-run
        assert out["questions"] == ["What system?", "Reversible?"]

    def test_keyword_safety_override(self):
        # agent says safe but keyword flagged a gated surface → fail closed
        out = cortex_audit(GATED, keyword_gated=True, enabled=True,
                           reviewer=lambda p: _verdict(decision="safe"))
        assert out["gated"] is True
        assert "safety_override" in out


class TestFailSafe:
    def test_disabled_uses_keyword(self):
        out = cortex_audit(GATED, keyword_gated=True, enabled=False, reviewer=lambda p: _verdict(decision="safe"))
        assert out["gated"] is True and out["source"] == "keyword-fallback"

    def test_reviewer_error_falls_back(self):
        def boom(p): raise RuntimeError("rail down")
        out = cortex_audit(SAFE, keyword_gated=False, enabled=True, reviewer=boom)
        assert out["source"] == "keyword-fallback" and out["gated"] is False
        assert "rail down" in out["fallback_reason"]

    def test_unparseable_falls_back(self):
        out = cortex_audit(GATED, keyword_gated=True, enabled=True, reviewer=lambda p: "no json here")
        assert out["source"] == "keyword-fallback" and out["gated"] is True

    def test_never_raises(self):
        # even with a totally broken reviewer return, returns a dict
        out = cortex_audit(SAFE, keyword_gated=False, enabled=True, reviewer=lambda p: None)
        assert isinstance(out, dict) and "gated" in out


class TestFlagAndPrompt:
    def test_flag_parsing(self):
        assert _flag_on("1") and _flag_on("true") and _flag_on("yes")
        assert not _flag_on("") and not _flag_on("0") and not _flag_on("off") and not _flag_on(None)

    def test_prompt_includes_ticket_and_rules(self):
        p = build_cortex_prompt(GATED)
        assert "AGENTS-2" in p and "Rotate API credentials" in p
        assert "FAIL CLOSED" in p and "needs-question" in p


def test_motor_integration_default_off_is_behavior_preserving(monkeypatch):
    """With the flag off (default), the motor's gate decision must equal the old
    keyword verdict — wiring is dormant until turn-on."""
    monkeypatch.delenv("TEAM_OS_CORTEX_AGENT", raising=False)
    # keyword says gated → cortex_audit (off) must return gated
    assert cortex_audit(GATED, keyword_gated=True)["gated"] is True
    assert cortex_audit(SAFE, keyword_gated=False)["gated"] is False
    assert cortex_audit(GATED, keyword_gated=True)["source"] == "keyword-fallback"
