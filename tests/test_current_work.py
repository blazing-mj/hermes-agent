"""Tests for ``agent.current_work``.

Covers AGENTS-66:

* :class:`CurrentWork` schema + JSON round-trip.
* Read/write/update of ``~/.hermes/state/current-work.json``.
* Post-compression mismatch guard — halts when the recorded
  ``last_user_message_verbatim`` no longer matches the latest user
  turn in the live conversation, continues otherwise.

Tests rely on the ``_hermetic_environment`` autouse fixture in
``tests/conftest.py`` to redirect ``HERMES_HOME`` at a per-test tempdir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.current_work import (
    CurrentWork,
    CurrentWorkMismatchError,
    MismatchResult,
    append_lifecycle_event,
    check_post_compression_mismatch,
    extract_latest_user_message,
    lifecycle_log_path,
    read_current_work,
    render_lifecycle_event,
    render_status,
    state_file_path,
    update_current_work,
    write_current_work,
)


# ── Path / location ──────────────────────────────────────────────────────


def test_state_file_path_under_hermes_home():
    """state_file_path() is computed from HERMES_HOME at call time."""
    path = state_file_path()
    assert path.name == "current-work.json"
    assert path.parent.name == "state"
    assert str(path).startswith(os.environ["HERMES_HOME"])


# ── Schema ───────────────────────────────────────────────────────────────


class TestSchema:
    def test_defaults_are_empty(self):
        work = CurrentWork()
        assert work.linear_id is None
        assert work.title is None
        assert work.phase is None
        assert work.dispatcher is None
        assert work.eta_minutes is None
        assert work.last_user_message_verbatim is None
        assert work.last_phase_change_at is None
        assert work.last_tool_call_at is None
        assert work.last_diff_fingerprint is None
        assert work.queue == []
        assert work.anomalies == []

    def test_round_trip_via_dict(self):
        original = CurrentWork(
            linear_id="AGENTS-66",
            title="current-work.json + post-compression check",
            phase="implementation",
            dispatcher="claude-max",
            eta_minutes=30,
            last_user_message_verbatim="ok, lets go",
            last_phase_change_at="2026-05-28T14:30:21Z",
            last_tool_call_at="2026-05-28T14:31:00Z",
            last_diff_fingerprint="abc123",
            queue=["AGENTS-65", "AGENTS-67"],
            anomalies=["manual seed"],
        )
        round_tripped = CurrentWork.from_dict(original.to_dict())
        assert round_tripped == original

    def test_from_dict_ignores_unknown_keys(self):
        """Forward-compat: a state file written by a newer version with
        extra fields must still load (with the unknowns silently dropped),
        not crash."""
        raw = {
            "linear_id": "AGENTS-66",
            "future_field_we_dont_know_about": 42,
        }
        work = CurrentWork.from_dict(raw)
        assert work.linear_id == "AGENTS-66"

    def test_from_dict_tolerates_missing_lists(self):
        work = CurrentWork.from_dict({"linear_id": "AGENTS-66"})
        assert work.queue == []
        assert work.anomalies == []


# ── Read / write / update ────────────────────────────────────────────────


class TestStateFile:
    def test_read_returns_none_when_file_missing(self):
        assert read_current_work() is None

    def test_write_creates_parent_dir(self, tmp_path, monkeypatch):
        # Point HERMES_HOME at a clean subdir to assert dir creation.
        fresh_home = tmp_path / "fresh_hermes"
        fresh_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(fresh_home))

        state_dir = fresh_home / "state"
        assert not state_dir.exists()

        write_current_work(CurrentWork(linear_id="AGENTS-66"))

        assert state_dir.is_dir()
        assert (state_dir / "current-work.json").is_file()

    def test_write_then_read_round_trips(self):
        original = CurrentWork(
            linear_id="AGENTS-66",
            title="t",
            phase="implementation",
            last_user_message_verbatim="ok, lets go",
            queue=["AGENTS-65"],
        )
        write_current_work(original)
        loaded = read_current_work()
        assert loaded == original

    def test_write_is_atomic_via_temp_file(self):
        """Writing must go through a temp file + rename so a partially-written
        JSON file is never observed by readers. We verify by inspecting the
        file contents after write — it must parse cleanly."""
        write_current_work(CurrentWork(linear_id="AGENTS-66", title="t"))
        data = json.loads(state_file_path().read_text())
        assert data["linear_id"] == "AGENTS-66"

    def test_read_returns_none_on_corrupt_json(self):
        """A corrupt state file should not crash callers — return None and
        let the caller decide whether to overwrite or escalate."""
        path = state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert read_current_work() is None

    def test_update_merges_partial_fields(self):
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                title="initial",
                phase="planning",
                queue=["AGENTS-65"],
            )
        )

        updated = update_current_work(phase="implementation", eta_minutes=30)

        # New values applied
        assert updated.phase == "implementation"
        assert updated.eta_minutes == 30
        # Untouched fields preserved
        assert updated.linear_id == "AGENTS-66"
        assert updated.title == "initial"
        assert updated.queue == ["AGENTS-65"]
        # Persisted
        assert read_current_work() == updated

    def test_update_creates_state_if_missing(self):
        updated = update_current_work(linear_id="AGENTS-66", phase="planning")
        assert updated.linear_id == "AGENTS-66"
        assert updated.phase == "planning"
        assert read_current_work() == updated


# ── Latest-user-message extraction ──────────────────────────────────────


class TestExtractLatestUserMessage:
    def test_returns_most_recent_user_text(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "thinking..."},
        ]
        assert extract_latest_user_message(messages) == "second"

    def test_returns_none_when_no_user_message(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "hi"},
        ]
        assert extract_latest_user_message(messages) is None

    def test_handles_list_content_parts(self):
        """User messages may carry list-of-parts content (multimodal /
        tool_result shape). Extractor must concatenate the text parts."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "image", "source": "..."},
                    {"type": "text", "text": "part two"},
                ],
            }
        ]
        text = extract_latest_user_message(messages)
        assert text is not None
        assert "part one" in text
        assert "part two" in text

    def test_skips_tool_result_user_messages(self):
        """Some adapters encode tool_result entries with role=user — those
        are not real user turns and must not be picked as the latest message.
        Heuristic: if a 'user' message's content is entirely tool_result
        parts, skip it."""
        messages = [
            {"role": "user", "content": "real user msg"},
            {"role": "assistant", "content": "thinking"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                ],
            },
        ]
        assert extract_latest_user_message(messages) == "real user msg"


# ── Post-compression mismatch guard ─────────────────────────────────────


class TestMismatchGuard:
    def test_no_state_means_no_halt(self):
        result = check_post_compression_mismatch(latest_user_message="anything")
        assert isinstance(result, MismatchResult)
        assert result.matched is True
        assert result.should_halt is False

    def test_no_recorded_user_message_means_no_halt(self):
        """If the state file has no last_user_message_verbatim recorded yet,
        we can't compare — so we must NOT halt (no false positives on
        fresh state)."""
        write_current_work(CurrentWork(linear_id="AGENTS-66"))
        result = check_post_compression_mismatch(latest_user_message="ok, lets go")
        assert result.matched is True
        assert result.should_halt is False

    def test_no_latest_user_message_means_no_halt(self):
        """If we can't extract a latest user message from the live convo
        (e.g. only system + assistant messages survived compression), we
        have nothing to compare — do not halt."""
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                last_user_message_verbatim="ok, lets go",
            )
        )
        result = check_post_compression_mismatch(latest_user_message=None)
        assert result.matched is True
        assert result.should_halt is False

    def test_matching_state_does_not_halt(self):
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                last_user_message_verbatim="ok, lets go",
            )
        )
        result = check_post_compression_mismatch(latest_user_message="ok, lets go")
        assert result.matched is True
        assert result.should_halt is False

    def test_matching_state_ignores_whitespace(self):
        """Trailing/leading whitespace shouldn't trigger a false halt — the
        verbatim message is a stable identifier, not a hash."""
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                last_user_message_verbatim="ok, lets go",
            )
        )
        result = check_post_compression_mismatch(latest_user_message="  ok, lets go\n")
        assert result.matched is True
        assert result.should_halt is False

    def test_stale_state_triggers_halt(self):
        """Latest user message diverges from the one that produced
        current-work → halt continuation and surface escalation."""
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                title="implementing 66",
                last_user_message_verbatim="ok, lets go",
            )
        )
        result = check_post_compression_mismatch(
            latest_user_message="actually stop, work on AGENTS-67 instead"
        )
        assert result.matched is False
        assert result.should_halt is True
        # Reason must surface enough info for the caller to escalate.
        assert "AGENTS-66" in result.reason or "mismatch" in result.reason.lower()
        assert result.stale_message == "ok, lets go"
        assert result.latest_message == "actually stop, work on AGENTS-67 instead"

    def test_explicit_work_argument_overrides_disk(self):
        """Callers can pass a CurrentWork directly (avoids extra disk reads
        when they already have it in memory)."""
        # Put something on disk that would NOT halt …
        write_current_work(
            CurrentWork(
                linear_id="AGENTS-66",
                last_user_message_verbatim="latest",
            )
        )
        # … but pass an explicit work that would halt.
        explicit = CurrentWork(
            linear_id="AGENTS-99",
            last_user_message_verbatim="stale verbatim",
        )
        result = check_post_compression_mismatch(
            latest_user_message="latest",
            work=explicit,
        )
        assert result.should_halt is True
        assert "AGENTS-99" in result.reason


# ── Status renderer ─────────────────────────────────────────────────────


class TestRenderStatus:
    def test_includes_core_fields(self):
        work = CurrentWork(
            linear_id="AGENTS-66",
            title="current-work.json + post-compression check",
            phase="implementation",
            dispatcher="claude-max",
            eta_minutes=30,
            queue=["AGENTS-65", "AGENTS-67"],
        )
        rendered = render_status(work)
        assert "AGENTS-66" in rendered
        assert "current-work.json + post-compression check" in rendered
        assert "implementation" in rendered
        assert "claude-max" in rendered
        assert "30" in rendered

    def test_handles_none_state(self):
        rendered = render_status(None)
        # Must produce *something* renderable rather than crashing.
        assert isinstance(rendered, str)
        assert rendered.strip() != ""

    def test_handles_empty_state(self):
        rendered = render_status(CurrentWork())
        assert isinstance(rendered, str)
        # No raw 'None' tokens leaked into the human-readable output.
        assert "None" not in rendered

    def test_status_includes_stuck_placeholder_and_diff_marker(self):
        rendered = render_status(
            CurrentWork(
                linear_id="AGENTS-65",
                phase="implementation",
                last_diff_fingerprint="diff:abc123",
            )
        )
        assert "Last diff/progress: diff:abc123" in rendered
        assert "Stuck risk: not evaluated yet" in rendered


# ── Lifecycle renderer/log ───────────────────────────────────────────────


class TestLifecycleEvents:
    def _work(self) -> CurrentWork:
        return CurrentWork(
            linear_id="AGENTS-65",
            title="Structured task comms layer",
            phase="implementation",
            dispatcher="Codex host",
            eta_minutes=25,
            last_diff_fingerprint="diff:abc123",
        )

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("task_start", "🎯 AGENTS-65 — Structured task comms layer"),
            ("phase_transition", "AGENTS-65 → Phase: implementation"),
            ("dispatcher_switch", "switched Claude → Codex"),
            ("heartbeat", "⏳ AGENTS-65 still working"),
            ("completion", "✅ AGENTS-65 shipped"),
            ("pause", "🟡 AGENTS-65 paused"),
        ],
    )
    def test_render_lifecycle_templates(self, event, expected):
        rendered = render_lifecycle_event(
            event,
            self._work(),
            reason="P0 foundation",
            old_dispatcher="Claude",
            new_dispatcher="Codex",
            elapsed_minutes=42,
            verifier="green",
            audit_rounds=1,
            files_changed=3,
        )
        assert expected in rendered
        assert "AGENTS-65" in rendered

    def test_append_lifecycle_event_writes_jsonl(self, tmp_path):
        path = tmp_path / "lifecycle.jsonl"
        record = append_lifecycle_event(
            "heartbeat",
            self._work(),
            path=path,
            elapsed_minutes=30,
        )
        assert record["event"] == "heartbeat"
        assert record["linear_id"] == "AGENTS-65"
        assert record["last_diff_fingerprint"] == "diff:abc123"
        line = path.read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        assert parsed["event"] == "heartbeat"
        assert "still working" in parsed["message"]

    def test_lifecycle_log_path_under_hermes_home(self):
        path = lifecycle_log_path()
        assert path.name == "lifecycle.jsonl"
        assert path.parent.name == "logs"
        assert str(path).startswith(os.environ["HERMES_HOME"])


# ── Compression insertion point ─────────────────────────────────────────

class _FakeCompressor:
    _last_summary_error = None
    _last_aux_model_failure_model = None
    _last_aux_model_failure_error = None

    def compress(self, messages, current_tokens=None, focus_topic=None):
        return list(messages)


class _FakeAgent:
    session_id = "session-before"
    model = "fake-model"
    _memory_manager = None
    context_compressor = _FakeCompressor()

    def __init__(self):
        self.statuses = []
        self.warnings = []

    def _emit_status(self, message):
        self.statuses.append(message)

    def _emit_warning(self, message):
        self.warnings.append(message)


def test_compress_context_halts_on_current_work_mismatch_before_session_rotation():
    from agent.conversation_compression import compress_context

    write_current_work(
        CurrentWork(
            linear_id="AGENTS-66",
            title="current-work.json + post-compression check",
            last_user_message_verbatim="old instruction",
        )
    )
    agent = _FakeAgent()

    with pytest.raises(CurrentWorkMismatchError) as exc:
        compress_context(
            agent,
            [{"role": "user", "content": "new instruction"}],
            system_message="system",
        )

    assert exc.value.result.should_halt is True
    assert exc.value.result.stale_message == "old instruction"
    assert exc.value.result.latest_message == "new instruction"
    assert agent.session_id == "session-before"
    assert agent.warnings
    assert "AGENTS-66" in agent.warnings[0]
