"""Gemini thinking-config must never set BOTH thinkingBudget and thinkingLevel.

Gemini 3.x rejects that combination (HTTP 400: "you can only set one of thinking
budget and thinking level"). When the request carried both, the model 400'd —
which, on the fallback leg, silently muted the bot the moment the primary model
blipped. The normaliser must keep exactly one (prefer the 3.x ``thinkingLevel``).
"""
from __future__ import annotations

import pytest

from agent.gemini_native_adapter import _normalize_thinking_config as native_norm
from agent.gemini_cloudcode_adapter import _normalize_thinking_config as cloudcode_norm


@pytest.mark.parametrize("normalize", [native_norm, cloudcode_norm])
class TestThinkingConfigMutuallyExclusive:
    def test_both_set_keeps_only_level(self, normalize):
        out = normalize({"thinkingBudget": 8000, "thinkingLevel": "high"})
        assert out is not None
        assert out.get("thinkingLevel") == "high"
        assert "thinkingBudget" not in out  # the fix: never both

    def test_both_set_snake_case_keeps_only_level(self, normalize):
        out = normalize({"thinking_budget": 8000, "thinking_level": "LOW"})
        assert out == {"thinkingLevel": "low"}

    def test_budget_only_is_kept(self, normalize):
        assert normalize({"thinkingBudget": 4096}) == {"thinkingBudget": 4096}

    def test_level_only_is_kept(self, normalize):
        assert normalize({"thinkingLevel": "high"}) == {"thinkingLevel": "high"}

    def test_include_thoughts_preserved_and_still_only_level(self, normalize):
        out = normalize(
            {"thinkingBudget": 8000, "thinkingLevel": "high", "includeThoughts": True}
        )
        assert out.get("thinkingLevel") == "high"
        assert "thinkingBudget" not in out
        assert out.get("includeThoughts") is True

    def test_empty_or_invalid_returns_none(self, normalize):
        assert normalize({}) is None
        assert normalize(None) is None
        assert normalize("nope") is None
