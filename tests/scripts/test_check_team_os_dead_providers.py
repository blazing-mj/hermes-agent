from __future__ import annotations

from pathlib import Path

import yaml

from scripts.check_team_os_dead_providers import scan_config


def _write_config(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return path


def test_flags_dead_providers_only_in_routing_chains(tmp_path):
    cfg = _write_config(
        tmp_path / "config.yaml",
        {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                {"provider": "openrouter", "model": "some/model"},
            ],
            "delegation": {"provider": "nous", "model": "hermes"},
            "auxiliary": {"compression": {"provider": "auto", "model": ""}},
            # Provider config sections are not routing chains and must not be flagged.
            "openrouter": {"min_coding_score": 0.65},
            "providers": {"nous": {"enabled": True}},
        },
    )

    findings = scan_config(cfg)

    assert {f["chain"] for f in findings} == {
        "fallback_providers[1].provider",
        "delegation.provider",
    }


def test_allows_current_team_os_chain_shape(tmp_path):
    cfg = _write_config(
        tmp_path / "config.yaml",
        {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                {"provider": "gemini", "model": "gemini-3.1-pro-preview"},
            ],
            "delegation": {"provider": "", "model": ""},
            "auxiliary": {
                "compression": {"provider": "auto", "model": ""},
                "approval": {"provider": "auto", "model": ""},
            },
            "openrouter": {"response_cache": True},
        },
    )

    assert scan_config(cfg) == []


def test_flags_provider_prefixes_in_model_strings(tmp_path):
    cfg = _write_config(
        tmp_path / "config.yaml",
        {
            "model": "openrouter/example-model",
            "fallback_model": [{"model": "nous/example-model"}],
            "auxiliary": {"title_generation": {"model": "anthropic/claude", "provider": "anthropic"}},
        },
    )

    findings = scan_config(cfg)

    assert {f["chain"] for f in findings} == {
        "model provider prefix",
        "fallback_model[0].model provider prefix",
    }
