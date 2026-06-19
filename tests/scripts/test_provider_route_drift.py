import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provider_route_drift.py"


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def run_scan(tmp_path: Path, config_text: str, baseline: dict):
    cfg = write_yaml(tmp_path / "config.yaml", config_text)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "scan",
            "--config",
            str(cfg),
            "--profile",
            "default",
            "--baseline",
            str(baseline_path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_approved_stock_routes_do_not_alert(tmp_path):
    config = """
model:
  provider: openai-codex
  default: gpt-5.5
fallback_providers:
  - provider: gemini
    model: gemini-3.1-pro-preview
auxiliary:
  compression:
    provider: auto
    model: ''
providers:
  anthropic: {}
  openai:
    model: gpt-5.5
"""
    baseline = {
        "default": {
            "routes": {
                "model.provider": "openai-codex",
                "model.default": "gpt-5.5",
                "fallback_providers[0].provider": "gemini",
                "fallback_providers[0].model": "gemini-3.1-pro-preview",
                "auxiliary.compression.provider": "auto",
                "providers.anthropic.present": True,
                "providers.openai.present": True,
                "providers.openai.model": "gpt-5.5",
            }
        }
    }
    result = run_scan(tmp_path, config, baseline)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "clean"
    assert payload["drift"] == []


def test_new_api_fallback_route_alerts(tmp_path):
    config = """
model:
  provider: openai-codex
  default: gpt-5.5
fallback_providers:
  - provider: gemini
    model: gemini-3.1-pro-preview
  - provider: anthropic
    model: claude-sonnet-4-6
"""
    baseline = {
        "default": {
            "routes": {
                "model.provider": "openai-codex",
                "model.default": "gpt-5.5",
                "fallback_providers[0].provider": "gemini",
                "fallback_providers[0].model": "gemini-3.1-pro-preview",
            }
        }
    }
    result = run_scan(tmp_path, config, baseline)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "drift"
    drift_by_field = {item["field"]: item for item in payload["drift"]}
    assert drift_by_field["fallback_providers[1].provider"] == {
        "profile": "default",
        "field": "fallback_providers[1].provider",
        "expected": None,
        "actual": "anthropic",
        "kind": "added",
        "impact": "fallback-only route",
    }


def test_changed_delegation_provider_alerts_without_printing_secrets(tmp_path):
    config = """
delegation:
  provider: anthropic
  model: claude-sonnet-4-6
providers:
  anthropic:
    key_env: ANTHROPIC_API_KEY
    api_key: should-not-appear
"""
    baseline = {
        "default": {
            "routes": {
                "delegation.provider": "openai-codex",
                "delegation.model": "gpt-5.5",
                "providers.anthropic.present": True,
                "providers.anthropic.key_env": "ANTHROPIC_API_KEY",
                "providers.anthropic.has_api_key": True,
            }
        }
    }
    result = run_scan(tmp_path, config, baseline)
    assert result.returncode == 2
    assert "should-not-appear" not in result.stdout
    payload = json.loads(result.stdout)
    fields = {d["field"]: d for d in payload["drift"]}
    assert fields["delegation.provider"]["kind"] == "changed"
    assert fields["delegation.provider"]["impact"] == "delegation/subagent route"


def test_generate_baseline_includes_profiles_and_redacted_key_markers(tmp_path):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "cortex"
    profile.mkdir(parents=True)
    write_yaml(root / "config.yaml", """
model:
  provider: openai-codex
  default: gpt-5.5
providers:
  openai:
    key_env: OPENAI_API_KEY
""")
    write_yaml(profile / "config.yaml", """
model:
  provider: anthropic
  default: claude-sonnet-4-6
auxiliary:
  vision:
    provider: auto
""")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "baseline", "--home", str(root), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"default", "cortex"}
    assert payload["default"]["routes"]["providers.openai.key_env"] == "OPENAI_API_KEY"
    assert "should-not-appear" not in json.dumps(payload)
    assert "has_api_key" not in json.dumps(payload)
    assert payload["cortex"]["routes"]["auxiliary.vision.provider"] == "auto"


def test_top_level_fallback_model_is_scanned_for_legacy_runtime_route(tmp_path):
    config = """
model:
  provider: openai-codex
  default: gpt-5.5
fallback_model: anthropic/claude-sonnet-4-6
"""
    baseline = {
        "default": {
            "routes": {
                "model.provider": "openai-codex",
                "model.default": "gpt-5.5",
            }
        }
    }
    result = run_scan(tmp_path, config, baseline)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    fields = {d["field"]: d for d in payload["drift"]}
    assert fields["fallback_model"]["impact"] == "fallback-only route"


def test_removed_baseline_profile_is_reported_as_drift(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    write_yaml(root / "config.yaml", """
model:
  provider: openai-codex
  default: gpt-5.5
""")
    baseline = {
        "default": {"routes": {"model.provider": "openai-codex", "model.default": "gpt-5.5"}},
        "old_profile": {"routes": {"model.provider": "anthropic", "model.default": "claude-sonnet-4-6"}},
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "scan", "--home", str(root), "--baseline", str(baseline_path), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert any(d["profile"] == "old_profile" and d["kind"] == "removed" for d in payload["drift"])


def test_secret_like_provider_and_auxiliary_keys_are_redacted(tmp_path):
    config = """
providers:
  oauthy:
    client_secret: provider-secret-value
    access_token: provider-token-value
    bearer_token: provider-bearer-value
    authorization: provider-auth-value
auxiliary:
  vision:
    provider: openai
    client_secret: aux-secret-value
    access_token: aux-token-value
"""
    baseline = {"default": {"routes": {}}}
    result = run_scan(tmp_path, config, baseline)
    assert result.returncode == 2
    for secret in [
        "provider-secret-value",
        "provider-token-value",
        "provider-bearer-value",
        "provider-auth-value",
        "aux-secret-value",
        "aux-token-value",
    ]:
        assert secret not in result.stdout
    payload = json.loads(result.stdout)
    fields = {d["field"]: d for d in payload["drift"]}
    assert fields["providers.oauthy.has_client_secret"]["actual"] is True
    assert fields["providers.oauthy.has_access_token"]["actual"] is True
    assert fields["auxiliary.vision.has_client_secret"]["actual"] is True


def test_top_level_fallback_model_mapping_and_list_forms_are_scanned(tmp_path):
    baseline = {"default": {"routes": {}}}
    mapping_result = run_scan(tmp_path, """
fallback_model:
  provider: anthropic
  model: claude-sonnet-4-6
""", baseline)
    assert mapping_result.returncode == 2
    mapping_payload = json.loads(mapping_result.stdout)
    mapping_fields = {d["field"]: d for d in mapping_payload["drift"]}
    assert mapping_fields["fallback_model.provider"]["actual"] == "anthropic"
    assert mapping_fields["fallback_model.provider"]["impact"] == "fallback-only route"
    assert mapping_fields["fallback_model.model"]["actual"] == "claude-sonnet-4-6"
    assert mapping_fields["fallback_model.model"]["impact"] == "fallback-only route"

    list_result = run_scan(tmp_path, """
fallback_model:
  - provider: anthropic
    model: claude-sonnet-4-6
  - provider: gemini
    model: gemini-3.1-pro-preview
""", baseline)
    assert list_result.returncode == 2
    list_payload = json.loads(list_result.stdout)
    list_fields = {d["field"]: d for d in list_payload["drift"]}
    assert list_fields["fallback_model[0].provider"]["actual"] == "anthropic"
    assert list_fields["fallback_model[0].provider"]["impact"] == "fallback-only route"
    assert list_fields["fallback_model[1].provider"]["actual"] == "gemini"
    assert list_fields["fallback_model[1].provider"]["impact"] == "fallback-only route"
