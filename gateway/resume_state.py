"""Durable gateway restart resume-state helpers.

The in-memory ``resume_pending`` flag preserves a session lane after a gateway
restart.  This module writes a separate operator-readable checkpoint before the
restart so an auto-resumed turn has an exact next step instead of only a generic
"summarize and continue" prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from agent.current_work import CurrentWork, read_current_work
from hermes_constants import get_hermes_home
from utils import atomic_json_write


_RESUME_STATE_RELATIVE_PATH = Path("state") / "restart-resume-state.json"


def restart_resume_state_path() -> Path:
    """Return the profile-aware restart resume checkpoint path."""

    return get_hermes_home() / _RESUME_STATE_RELATIVE_PATH


def _read_all(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"version": 1, "sessions": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "sessions": {}}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        raw["sessions"] = {}
    raw.setdefault("version", 1)
    return raw


def _coerce_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [str(value) for value in values if str(value).strip()]
    text = str(values).strip()
    return [text] if text else []


def build_restart_resume_state(
    session_key: str,
    reason: str,
    *,
    current_work: CurrentWork | None = None,
    completed_steps: list[str] | None = None,
    next_step: str | None = None,
    proof_pending: list[str] | None = None,
) -> dict[str, Any]:
    """Build the durable RESUME-STATE payload for one gateway session.

    ``CurrentWork.queue`` is treated as the canonical unfinished checklist when
    available: queue[0] is the exact next step and the whole queue is the proof
    still pending.  If the queue is empty we still persist a conservative next
    step so the resumed turn continues the transcript rather than acking only.
    """

    work = current_work if current_work is not None else read_current_work()
    queue = list(work.queue) if work and work.queue else []
    inferred_completed = _coerce_list(completed_steps)
    if not inferred_completed and work and work.phase:
        inferred_completed = [str(work.phase)]

    inferred_next = (next_step or (queue[0] if queue else None) or "continue the interrupted work from the preserved transcript").strip()
    inferred_proof = _coerce_list(proof_pending) or queue or [inferred_next]

    return {
        "session_key": session_key,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "ticket_id": work.linear_id if work else None,
        "title": work.title if work else None,
        "phase": work.phase if work else None,
        "completed_steps": inferred_completed,
        "next_step": inferred_next,
        "proof_pending": inferred_proof,
        "restart_policy": "restart is the last action unless this is a deliberate resume-proof harness",
    }


def write_restart_resume_state(
    session_key: str,
    reason: str = "restart_timeout",
    *,
    current_work: CurrentWork | None = None,
    completed_steps: list[str] | None = None,
    next_step: str | None = None,
    proof_pending: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Atomically persist one session's pre-restart RESUME-STATE."""

    target = path or restart_resume_state_path()
    data = _read_all(target)
    state = build_restart_resume_state(
        session_key,
        reason,
        current_work=current_work,
        completed_steps=completed_steps,
        next_step=next_step,
        proof_pending=proof_pending,
    )
    data["updated_at"] = state["written_at"]
    data.setdefault("sessions", {})[session_key] = state
    atomic_json_write(target, data, indent=2)
    return state


def read_restart_resume_state(session_key: str, *, path: Path | None = None) -> dict[str, Any] | None:
    """Read the most recent RESUME-STATE payload for ``session_key``."""

    target = path or restart_resume_state_path()
    data = _read_all(target)
    state = data.get("sessions", {}).get(session_key)
    return state if isinstance(state, dict) else None
