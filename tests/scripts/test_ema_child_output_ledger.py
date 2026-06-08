"""Tests for EMA child-output contract verifier (AGENTS-188)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ema_child_output_ledger.py"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ema_child_output_ledger"
SESSION_FIXTURE = FIXTURE_DIR / "jun7_redacted_failure_recovery.jsonl"
SINGLE_FIXTURE = FIXTURE_DIR / "single_expected_outputs.jsonl"


def load_mod():
    spec = importlib.util.spec_from_file_location("ema_child_output_ledger", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ok_artifact_with_status_header_is_ok():
    mod = load_mod()
    ledger = mod.build_ledger(SINGLE_FIXTURE, artifact_root=FIXTURE_DIR / "artifacts_ok")

    ok = [row for row in ledger if row["output_name"] == "q1q2-what-we-did.md"]
    assert len(ok) == 1
    assert ok[0]["status"] == "OK"
    assert ok[0]["status_header"] == "STATUS: OK | reason: Complete Q1+Q2 test inventory compiled"


def test_missing_expected_artifact_fails_closed_as_missing_output():
    mod = load_mod()
    ledger = mod.build_ledger(SINGLE_FIXTURE, artifact_root=FIXTURE_DIR / "artifacts_missing")

    miss = [row for row in ledger if row["output_name"] == "q1q2-klaviyo-metrics.md"]
    assert len(miss) == 1
    assert miss[0]["status"] == "MISSING_OUTPUT"
    assert miss[0]["fail_closed"] is True


def test_artifact_without_status_header_fails_closed_as_no_status():
    mod = load_mod()
    ledger = mod.build_ledger(SINGLE_FIXTURE, artifact_root=FIXTURE_DIR / "artifacts_no_status")

    no_status = [row for row in ledger if row["output_name"] == "q1q2-klaviyo-metrics.md"]
    assert len(no_status) == 1
    assert no_status[0]["status"] == "NO_STATUS"
    assert no_status[0]["fail_closed"] is True
    assert no_status[0]["status_header"] == ""


def test_respawn_sequence_preserves_earlier_miss_and_final_success():
    mod = load_mod()
    ledger = mod.build_ledger(SESSION_FIXTURE, artifact_root=FIXTURE_DIR / "artifacts_recovered")

    metrics = [row for row in ledger if row["output_name"] == "q1q2-klaviyo-metrics.md"]
    assert [row["attempt"] for row in metrics] == [1, 2]
    assert [row["status"] for row in metrics] == ["MISSING_OUTPUT", "OK"]
    assert metrics[0]["evidence"] == "parent_read_error_after_completion"
    assert metrics[1]["status_header"].startswith("STATUS: OK")


def test_cli_emits_deterministic_json_ledger(capsys):
    mod = load_mod()

    code = mod.main([
        "--session-jsonl",
        str(SESSION_FIXTURE),
        "--artifact-root",
        str(FIXTURE_DIR / "artifacts_recovered"),
    ])

    assert code == 0
    output = capsys.readouterr().out
    rows = json.loads(output)
    assert rows[0]["child_session_key"] == "agent:ema-data-analyst:subagent:first"
    assert rows[0]["status"] == "MISSING_OUTPUT"
    assert rows[-1]["status"] == "OK"
