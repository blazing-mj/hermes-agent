"""Phase 0.5 — gateway health watcher (scripts/gateway_health_watch.py).

Pure-detector tests (no I/O, no tokens) + cursor/cooldown behavior.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path("/Users/alfred/.hermes/hermes-agent")
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import gateway_health_watch as ghw  # noqa: E402

NOW = 1_700_000_000.0


def _inp(**kw):
    base = {"now": NOW, "newest_log_ts": NOW - 60, "new_error_lines": []}
    base.update(kw)
    return ghw.WatchInputs(**base)


class TestSilence:
    def test_stale_heartbeat_alerts(self):
        a = ghw.detect(_inp(newest_log_ts=NOW - 1200))  # 20 min old
        assert [x.kind for x in a] == ["silent"]

    def test_fresh_heartbeat_no_alert(self):
        assert ghw.detect(_inp(newest_log_ts=NOW - 120)) == []

    def test_no_timestamp_does_not_alert(self):
        # if we can't read a heartbeat at all, don't fabricate a silence alert
        assert not any(x.kind == "silent" for x in ghw.detect(_inp(newest_log_ts=None)))

    def test_boundary_just_under_threshold(self):
        assert ghw.detect(_inp(newest_log_ts=NOW - (ghw.HEARTBEAT_MAX_AGE_SEC - 5))) == []


class TestModelChainFailure:
    def test_terminal_burst_alerts(self):
        lines = ["resolve_provider_client: openai-codex requested but no Codex OAuth token"] * 3
        a = ghw.detect(_inp(new_error_lines=lines))
        assert any(x.kind == "model_chain_failed" for x in a)

    def test_recovered_burst_suppressed(self):
        lines = ["no Codex OAuth token"] * 3 + ["falling back to anthropic/claude-sonnet-4-6"]
        assert not any(x.kind == "model_chain_failed" for x in ghw.detect(_inp(new_error_lines=lines)))

    def test_transient_fallback_not_alerted(self):
        # normal openrouter/nous fallback churn must never alert
        lines = ["Auxiliary: marking openrouter unhealthy"] * 5
        assert ghw.detect(_inp(new_error_lines=lines)) == []

    def test_below_burst_threshold_no_alert(self):
        a = ghw.detect(_inp(new_error_lines=["token is expired"]))  # only 1
        assert not any(x.kind == "model_chain_failed" for x in a)

    def test_auth_expired_burst_alerts(self):
        lines = ["API call failed error_type=AuthenticationError 401 token is expired"] * 3
        assert any(x.kind == "model_chain_failed" for x in ghw.detect(_inp(new_error_lines=lines)))

    def test_transport_noise_does_not_suppress_real_outage(self):
        # a channel reconnect ("✓ telegram connected") is NOT model recovery —
        # it must NOT suppress a genuine model-chain outage alert (false-negative guard)
        lines = ["no Codex OAuth token"] * 3 + ["✓ telegram connected", "response sent"]
        assert any(x.kind == "model_chain_failed" for x in ghw.detect(_inp(new_error_lines=lines)))


class TestTimestampParsing:
    def test_parses_real_format(self):
        assert ghw.parse_ts("2026-06-27 20:17:05,760 INFO gateway.memory_monitor: [MEMORY") is not None

    def test_non_timestamp_line(self):
        assert ghw.parse_ts("    File \"x.py\", line 5, in run") is None


class TestCursorAndCooldown:
    def test_cursor_reads_only_new_lines(self, tmp_path, monkeypatch):
        log = tmp_path / "e.log"
        log.write_text("line1\nline2\n")
        state = {}
        first = ghw._read_new_lines(log, "c", state)
        assert first == ["line1", "line2"]
        log.write_text("line1\nline2\nline3\n")
        second = ghw._read_new_lines(log, "c", state)
        assert second == ["line3"]  # only the appended line

    def test_cursor_handles_rotation(self, tmp_path):
        log = tmp_path / "e.log"
        log.write_text("a\nb\nc\n")
        state = {}
        ghw._read_new_lines(log, "c", state)
        log.write_text("x\n")  # truncated/rotated (smaller)
        assert ghw._read_new_lines(log, "c", state) == ["x"]

    def test_cooldown_suppresses_repeat(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ghw, "STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(ghw, "GATEWAY_LOG", tmp_path / "gw.log")
        monkeypatch.setattr(ghw, "ERROR_LOG", tmp_path / "err.log")
        # stale heartbeat in the gateway log → silent alert
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        (tmp_path / "gw.log").write_text(f"{old} INFO gateway.memory_monitor: beat\n")
        (tmp_path / "err.log").write_text("")
        r1 = ghw.run_tick(dry_run=True)
        assert "silent" in [k for k, _ in r1["alerts"]]


def test_self_test_passes():
    assert ghw._self_test() == 0
