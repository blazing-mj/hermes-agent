"""Tests for the EMA child-output observation helper (AGENTS-206)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ema_child_output_observe.py"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ema_child_output_ledger"
SESSION_FIXTURE = FIXTURE_DIR / "single_expected_outputs.jsonl"


def load_mod():
    spec = importlib.util.spec_from_file_location("ema_child_output_observe", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discover_candidates_is_bounded_and_read_only(tmp_path):
    home = tmp_path / ".openclaw"
    sessions = home / "agents" / "ema" / "sessions-archive"
    sessions.mkdir(parents=True)
    (sessions / "b.jsonl").write_text('{"message": {}}\n', encoding="utf-8")
    (sessions / "a.trajectory.jsonl").write_text('{"message": {}}\n', encoding="utf-8")
    artifact_root = home / "workspace-agency" / "roles" / "email-strategist" / "instances" / "ema" / "tmp"
    artifact_root.mkdir(parents=True)
    (artifact_root / "child-output.md").write_text("STATUS: OK\n", encoding="utf-8")

    mod = load_mod()
    result = mod.discover_candidates(home, limit=1)

    assert result["ema_sessions"] == [str(sessions / "b.jsonl")]
    assert result["artifact_roots"] == [str(artifact_root)]


def test_cli_runs_ledger_when_paths_are_supplied(capsys):
    mod = load_mod()

    code = mod.main([
        "--session-jsonl",
        str(SESSION_FIXTURE),
        "--artifact-root",
        str(FIXTURE_DIR / "artifacts_ok"),
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(row["status"] == "OK" for row in payload["ledger"])
    assert payload["ledger_observation_status"] == "ledger_rows_found"
    assert payload["production_observation_status"] == "pending_next_normal_live_EMA_test"
