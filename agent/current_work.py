"""Canonical active-work state for long-running Hermes dispatch.

This module owns the tiny latest-state file at
``{HERMES_HOME}/state/current-work.json``.  It is intentionally small: the file
is a current truth anchor, not a narrative log.  Gateway status, structured task
updates, post-compression checks, and future stuck-task detection should all read
from this same source instead of inferring active work from stale conversation
history.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from utils import atomic_json_write


_STATE_RELATIVE_PATH = Path("state") / "current-work.json"
_LIFECYCLE_LOG_RELATIVE_PATH = Path("logs") / "lifecycle.jsonl"
_LIFECYCLE_EVENTS = {
    "task_start",
    "phase_transition",
    "dispatcher_switch",
    "heartbeat",
    "completion",
    "pause",
}


def state_file_path() -> Path:
    """Return the profile-aware current-work state path.

    The path is resolved at call time so tests and profile-specific gateway
    processes can set ``HERMES_HOME`` without fighting module-level globals.
    """

    return get_hermes_home() / _STATE_RELATIVE_PATH


def lifecycle_log_path() -> Path:
    """Return the profile-aware task lifecycle JSONL log path."""

    return get_hermes_home() / _LIFECYCLE_LOG_RELATIVE_PATH


@dataclass(slots=True)
class CurrentWork:

    """Latest active-work state.

    Keep this schema intentionally compact.  It is safe for newer writers to add
    fields because ``from_dict`` ignores unknown keys.
    """

    linear_id: str | None = None
    title: str | None = None
    phase: str | None = None
    dispatcher: str | None = None
    eta_minutes: int | None = None
    last_user_message_verbatim: str | None = None
    last_phase_change_at: str | None = None
    last_tool_call_at: str | None = None
    last_diff_fingerprint: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_phase: str | None = None
    heartbeat_diff_fingerprint: str | None = None
    heartbeat_streak: int = 0
    stuck_signal_at: str | None = None
    stuck_reason: str | None = None
    queue: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            field_def.name: list(value) if isinstance(value, list) else value
            for field_def in fields(self)
            if (value := getattr(self, field_def.name)) is not None
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CurrentWork":
        if not isinstance(raw, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        for key in ("queue", "anomalies"):
            value = data.get(key)
            if value is None:
                data[key] = []
            elif isinstance(value, list):
                data[key] = [str(v) for v in value]
            else:
                data[key] = [str(value)]
        eta = data.get("eta_minutes")
        if eta is not None and not isinstance(eta, int):
            try:
                data["eta_minutes"] = int(eta)
            except (TypeError, ValueError):
                data["eta_minutes"] = None
        return cls(**data)


def read_current_work(path: Path | None = None) -> CurrentWork | None:
    """Read current-work state, returning ``None`` if absent/corrupt.

    Corrupt state should not crash the caller during recovery paths.  Callers can
    decide whether to overwrite, render an anomaly, or escalate.
    """

    path = path or state_file_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return CurrentWork.from_dict(raw)


def write_current_work(work: CurrentWork, path: Path | None = None) -> None:
    """Persist current-work state atomically."""

    atomic_json_write(path or state_file_path(), work.to_dict(), indent=2)


def update_current_work(path: Path | None = None, **updates: Any) -> CurrentWork:
    """Merge fields into current-work state and persist the result."""

    current = read_current_work(path) or CurrentWork()
    data = current.to_dict()
    known = {f.name for f in fields(CurrentWork)}
    for key, value in updates.items():
        if key in known:
            data[key] = value
    updated = CurrentWork.from_dict(data)
    write_current_work(updated, path)
    return updated


def _text_from_content_parts(parts: Iterable[Any]) -> tuple[str, bool]:
    """Return text from content parts plus whether a real text part existed."""

    text_parts: list[str] = []
    saw_text = False
    for part in parts:
        if isinstance(part, str):
            if part:
                saw_text = True
                text_parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_result":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            saw_text = True
            text_parts.append(text)
    return "\n".join(text_parts).strip(), saw_text


def extract_latest_user_message(messages: list[dict[str, Any]] | None) -> str | None:
    """Extract the latest real user text from an OpenAI-style message list.

    Tool-result-only user messages are skipped because some adapters encode tool
    results as ``role=user``; those must not overwrite MJ's latest instruction.
    """

    if not messages:
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            stripped = content.strip()
            return stripped or None
        if isinstance(content, list):
            text, saw_text = _text_from_content_parts(content)
            if saw_text:
                return text or None
            continue
        if content is not None:
            text = str(content).strip()
            if text:
                return text
    return None


@dataclass(slots=True)
class StuckCheckResult:
    """Outcome from recording one active-work heartbeat."""

    stuck: bool
    streak: int
    phase: str | None
    last_diff_fingerprint: str | None
    message: str | None = None


class CurrentWorkMismatchError(RuntimeError):
    """Raised when post-compression state disagrees with live user intent."""

    def __init__(self, result: "MismatchResult") -> None:
        self.result = result
        super().__init__(result.reason)


@dataclass(slots=True)
class MismatchResult:
    """Result of comparing persisted active-work state to live user intent."""

    matched: bool
    should_halt: bool
    reason: str = ""
    stale_message: str | None = None
    latest_message: str | None = None
    work: CurrentWork | None = None


def _norm_message(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()


def check_post_compression_mismatch(
    *,
    latest_user_message: str | None,
    work: CurrentWork | None = None,
) -> MismatchResult:
    """Compare active-work state with the latest user message after compaction.

    Missing state or missing comparable messages should not halt: false positives
    would be worse than no guard on fresh sessions.  A mismatch means the compacted
    handoff may be stale relative to MJ's latest instruction, so callers should
    stop continuation and surface status/escalation.
    """

    work = work if work is not None else read_current_work()
    if work is None:
        return MismatchResult(matched=True, should_halt=False, reason="no current-work state")

    stale = _norm_message(work.last_user_message_verbatim)
    latest = _norm_message(latest_user_message)
    if not stale:
        return MismatchResult(matched=True, should_halt=False, reason="current-work has no recorded user message", work=work)
    if not latest:
        return MismatchResult(matched=True, should_halt=False, reason="no latest user message available", stale_message=stale, work=work)
    if stale == latest:
        return MismatchResult(matched=True, should_halt=False, reason="current-work matches latest user message", stale_message=stale, latest_message=latest, work=work)

    ident = work.linear_id or "active work"
    return MismatchResult(
        matched=False,
        should_halt=True,
        reason=(
            f"current-work mismatch for {ident}: persisted last user message "
            "differs from latest live user message; stop stale continuation and surface /status."
        ),
        stale_message=stale,
        latest_message=latest,
        work=work,
    )


def check_post_compression_messages(messages: list[dict[str, Any]]) -> MismatchResult:
    """Convenience wrapper for compression/session-hygiene callers."""

    return check_post_compression_mismatch(
        latest_user_message=extract_latest_user_message(messages)
    )


def record_heartbeat(work: CurrentWork | None = None, *, now: str | None = None) -> StuckCheckResult:
    """Record one 30-minute heartbeat and detect unchanged phase+diff streaks.

    Detection deliberately uses only ``CurrentWork.phase`` and
    ``CurrentWork.last_diff_fingerprint`` as MJ requested.  The extra heartbeat
    fields store comparison state; they are not an alternate progress signal.
    """

    work = work or read_current_work() or CurrentWork()
    now = now or datetime.now(timezone.utc).isoformat()
    same_phase = work.phase == work.heartbeat_phase
    same_diff = work.last_diff_fingerprint == work.heartbeat_diff_fingerprint
    has_baseline = bool(work.heartbeat_phase or work.heartbeat_diff_fingerprint)
    if has_baseline and same_phase and same_diff:
        streak = max(1, int(work.heartbeat_streak or 1)) + 1
    else:
        streak = 1
        work.stuck_signal_at = None
        work.stuck_reason = None

    work.last_heartbeat_at = now
    work.heartbeat_phase = work.phase
    work.heartbeat_diff_fingerprint = work.last_diff_fingerprint
    work.heartbeat_streak = streak

    stuck = streak >= 2 and has_baseline and same_phase and same_diff
    message = None
    if stuck:
        ident = _val(work.linear_id, "Active task")
        message = f"⚠️ {ident} may be stuck — same phase 60min, no diff progress"
        work.stuck_signal_at = now
        work.stuck_reason = message
        if message not in work.anomalies:
            work.anomalies.append(message)
    write_current_work(work)
    append_lifecycle_event(
        "heartbeat",
        work,
        elapsed_minutes=30 * streak,
        stuck_signal=stuck,
    )
    return StuckCheckResult(
        stuck=stuck,
        streak=streak,
        phase=work.phase,
        last_diff_fingerprint=work.last_diff_fingerprint,
        message=message,
    )


def _val(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def render_lifecycle_event(
    event: str,
    work: CurrentWork | None = None,
    *,
    elapsed_minutes: int | None = None,
    old_dispatcher: str | None = None,
    new_dispatcher: str | None = None,
    reason: str | None = None,
    verifier: str | None = None,
    audit_rounds: int | None = None,
    files_changed: int | None = None,
) -> str:
    """Render a task-aware lifecycle stamp for Telegram/local logs."""

    if event not in _LIFECYCLE_EVENTS:
        raise ValueError(f"unknown lifecycle event: {event}")
    work = work or CurrentWork()
    ident = _val(work.linear_id, "active task")
    title = _val(work.title)
    phase = _val(work.phase)
    dispatcher = _val(work.dispatcher)
    eta = f"{work.eta_minutes}m" if work.eta_minutes is not None else "unknown"

    if event == "task_start":
        return (
            f"🎯 {ident} — {title}\n"
            f"   Dispatcher: {dispatcher}\n"
            f"   Why: {_val(reason, 'not recorded')}\n"
            f"   Phase: {phase}\n"
            f"   ETA: {eta}"
        )
    if event == "phase_transition":
        suffix = f", ETA {eta}" if work.eta_minutes is not None else ""
        return f"{ident} → Phase: {phase} (started{suffix})"
    if event == "dispatcher_switch":
        old = _val(old_dispatcher, dispatcher)
        new = _val(new_dispatcher, dispatcher)
        return f"{ident} → switched {old} → {new} (reason: {_val(reason, 'not recorded')})"
    if event == "heartbeat":
        elapsed = f", {elapsed_minutes}min elapsed" if elapsed_minutes is not None else ""
        return f"⏳ {ident} still working — phase: {phase}{elapsed}"
    if event == "completion":
        elapsed = f" — {elapsed_minutes}min" if elapsed_minutes is not None else ""
        lines = [
            f"✅ {ident} shipped{elapsed}",
            f"   Dispatcher used: {dispatcher}",
            f"   Verifier: {_val(verifier, 'not recorded')}, audit rounds: {_val(audit_rounds, 'unknown')}, files: {_val(files_changed, 'unknown')} changed",
        ]
        return "\n".join(lines)
    if event == "pause":
        return f"🟡 {ident} paused — {_val(reason, 'awaiting input')}"
    raise AssertionError("unreachable")


def append_lifecycle_event(
    event: str,
    work: CurrentWork | None = None,
    *,
    path: Path | None = None,
    **details: Any,
) -> dict[str, Any]:
    """Append one lifecycle event as JSONL and return the written record."""

    if event not in _LIFECYCLE_EVENTS:
        raise ValueError(f"unknown lifecycle event: {event}")
    work = work or read_current_work() or CurrentWork()
    render_details = {
        key: details[key]
        for key in (
            "elapsed_minutes",
            "old_dispatcher",
            "new_dispatcher",
            "reason",
            "verifier",
            "audit_rounds",
            "files_changed",
        )
        if key in details
    }
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "linear_id": work.linear_id,
        "title": work.title,
        "phase": work.phase,
        "dispatcher": work.dispatcher,
        "eta_minutes": work.eta_minutes,
        "last_diff_fingerprint": work.last_diff_fingerprint,
        "message": render_lifecycle_event(event, work, **render_details),
    }
    for key, value in details.items():
        if value is not None:
            record[key] = value
    log_path = path or lifecycle_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def render_status(work: CurrentWork | None = None) -> str:
    """Render a compact human-readable status snapshot from current-work state."""

    if work is None:
        work = read_current_work()
    if work is None:
        return "No active work recorded."

    lines = [
        f"Active task: {_val(work.linear_id)} — {_val(work.title)}",
        f"Phase: {_val(work.phase)}",
        f"Dispatcher: {_val(work.dispatcher)}",
        f"ETA: {_val(work.eta_minutes)}min" if work.eta_minutes is not None else "ETA: unknown",
        f"Queue: {', '.join(work.queue) if work.queue else 'empty'}",
        f"Last user message: {_val(work.last_user_message_verbatim, 'not recorded')}",
        f"Last phase change: {_val(work.last_phase_change_at, 'not recorded')}",
        f"Last tool call: {_val(work.last_tool_call_at, 'not recorded')}",
        f"Last diff/progress: {_val(work.last_diff_fingerprint, 'not recorded')}",
        f"Last heartbeat: {_val(work.last_heartbeat_at, 'not recorded')}",
        f"Heartbeat streak: {work.heartbeat_streak}",
        f"Stuck risk: {work.stuck_reason or 'clear'}",
    ]
    if work.anomalies:
        lines.append("Anomalies:")
        lines.extend(f"- {item}" for item in work.anomalies)
    else:
        lines.append("Anomalies: none")
    return "\n".join(lines)
