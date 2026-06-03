from __future__ import annotations

from pathlib import Path

SCRIPT = Path("/Users/alfred/.hermes/scripts/cortex_autonomous_triage_ticketing.sh")


def test_cortex_triage_uses_no_tools_classifier_and_restricted_writer():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--no-tools" in text
    assert "restricted_linear_writer.py" in text
    assert "-t terminal,file,search" not in text
    assert "linear-agent" not in text


def test_cortex_triage_prompt_does_not_grant_tool_or_linear_authority():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "You have no tools" in text
    assert "Allowed action types: list, issue, create, comment" in text
    assert "mark implementation work Done" in text
    assert "/Users/alfred/.hermes/bin/linear-agent" not in text
