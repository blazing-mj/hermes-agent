"""Event-driven Linear approval webhook handling for Team OS.

This module is intentionally deterministic and small: Linear webhooks are the
wake-up signal, not a polling loop. It handles only the MJ decision lanes and
Needs-MJ comments, then records the result in the Team OS outbox and Linear.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .db import TeamOSState

AddComment = Callable[[str, str], None]
RunIntakeWake = Callable[..., dict[str, Any]]

_APPROVED = "approved"
_REJECTED = "rejected"
_NOT_APPROVED = "not approved"
_QUESTION = "question"
_NEEDS_MJ = "needs-mj"


def _state_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("id") or ""
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _state_name(value).strip().lower()


def _issue_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    if _norm(payload.get("type")) == "comment":
        raw_issue = data.get("issue")
        issue: dict[str, Any] = raw_issue if isinstance(raw_issue, dict) else {}
        return issue
    return data


def _issue_identifier(payload: dict[str, Any]) -> str:
    issue = _issue_from_payload(payload)
    return str(issue.get("identifier") or issue.get("id") or "").strip()


def _issue_title(payload: dict[str, Any]) -> str:
    issue = _issue_from_payload(payload)
    return str(issue.get("title") or _issue_identifier(payload) or "Team OS card").strip()


def _current_state(payload: dict[str, Any]) -> str:
    issue = _issue_from_payload(payload)
    return _state_name(issue.get("state"))


def _previous_state(payload: dict[str, Any]) -> str:
    raw_updated_from = payload.get("updatedFrom")
    updated_from: dict[str, Any] = raw_updated_from if isinstance(raw_updated_from, dict) else {}
    return _state_name(updated_from.get("state"))


def _event_action(payload: dict[str, Any]) -> str:
    return str(payload.get("action") or payload.get("webhookAction") or "").strip().lower()


def _is_backlog_intake_doorbell(payload: dict[str, Any]) -> bool:
    """Return true for Linear Issue events that should wake Cortex intake."""

    if _norm(payload.get("type")) != "issue":
        return False
    if _norm(_current_state(payload)) != "backlog":
        return False
    action = _event_action(payload)
    if action == "create":
        return True
    return action in {"update", ""} and _norm(_previous_state(payload)) != "backlog"


def run_cortex_intake_wake(*, issue_id: str, wake_source: str = "doorbell") -> dict[str, Any]:
    """Start the Cortex safe-work intake runner asynchronously."""

    script = Path(os.environ.get("TEAM_OS_CORTEX_INTAKE_SCRIPT", "/Users/alfred/.hermes/scripts/cortex_work_intake_dispatch.sh"))
    if not script.exists():
        return {"started": False, "reason": "intake script missing", "script": str(script)}
    env = os.environ.copy()
    env["TEAM_OS_INTAKE_WAKE_SOURCE"] = wake_source
    env["TEAM_OS_INTAKE_WAKE_ISSUE"] = issue_id
    proc = subprocess.Popen(
        [str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    return {"started": True, "pid": proc.pid, "script": str(script)}


def _latest_comment_body(payload: dict[str, Any]) -> str:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    if _norm(payload.get("type")) == "comment":
        return str(data.get("body") or "").strip()
    raw_comments = data.get("comments")
    comments: dict[str, Any] = raw_comments if isinstance(raw_comments, dict) else {}
    raw_nodes = comments.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    if nodes and isinstance(nodes[-1], dict):
        return str(nodes[-1].get("body") or "").strip()
    return ""


def _decision_from_comment(body: str) -> str:
    """Return an MJ decision command at the front of a Linear comment, if any."""
    text = " ".join(str(body or "").strip().lower().split())
    if not text:
        return ""
    # Reject must precede Approved so "NOT APPROVED" does not get treated as approved.
    if text.startswith("not approved") or text.startswith("rejected") or text.startswith("reject"):
        return _REJECTED
    if text.startswith("approved") or text.startswith("approve"):
        return _APPROVED
    if text.startswith("question") or text.startswith("?"):
        return _QUESTION
    return ""


def _is_self_comment(body: str) -> bool:
    text = str(body or "").strip().lower()
    return text.startswith((
        "answer for ",
        "approved received ",
        "rejected received ",
    ))


def _is_needs_mj_context(payload: dict[str, Any], row: dict[str, Any] | None) -> bool:
    if row and row.get("state") == "mj_review":
        return True
    return _norm(_current_state(payload)) == _NEEDS_MJ or _norm(_previous_state(payload)) == _NEEDS_MJ


def _update_outbox_payload_and_state(
    state: TeamOSState,
    row: dict[str, Any],
    *,
    new_state: str,
    note: str = "",
    last_error: str = "",
) -> None:
    payload = dict(row.get("payload") or {})
    if note:
        payload["mj_notes"] = note
    payload["mj_decision_at"] = int(time.time())
    payload["mj_decision_state"] = new_state
    now = int(time.time())
    with state.connect() as conn:
        conn.execute(
            """
            UPDATE outbox
            SET state = ?, updated_at = ?, queued_at = CASE WHEN ? = 'queued' THEN ? ELSE queued_at END,
                completed_at = CASE WHEN ? IN ('failed', 'abandoned', 'succeeded') THEN ? ELSE completed_at END,
                payload_json = ?, last_error = NULLIF(?, '')
            WHERE id = ?
            """,
            (
                new_state,
                now,
                new_state,
                now,
                new_state,
                now,
                json.dumps(payload, sort_keys=True),
                last_error,
                int(row["id"]),
            ),
        )
        conn.commit()


def _question_reply(issue_id: str, title: str, comment_body: str) -> str:
    question_line = f"\n\nMJ question captured: {comment_body}" if comment_body else ""
    return (
        f"Answer for {issue_id} — {title}:\n\n"
        "You are approving whether Team OS may continue past this human gate.\n\n"
        "Plain-language gate summary should tell you: Problem → fix → new behavior → what approval allows.\n\n"
        "Move the card to Approved to continue. Add a comment before/with Approved if you want constraints carried forward. "
        "Move it to Rejected with a comment if it should revise/archive instead."
        f"{question_line}"
    )


def handle_linear_webhook(
    payload: dict[str, Any],
    *,
    state: TeamOSState,
    add_comment: AddComment,
    run_intake_wake: RunIntakeWake = run_cortex_intake_wake,
) -> dict[str, Any]:
    """Handle one Linear webhook payload for Team OS approval UX.

    Supported events:
    - Issue moved from Needs-MJ to Approved/Rejected/Question.
    - Comment created on a Needs-MJ issue.

    Low-cost or unrelated cards are ignored: no assignment, no ping, no comment.
    """
    issue_id = _issue_identifier(payload)
    if not issue_id:
        return {"decision": "ignored", "reason": "missing issue identifier"}

    if _is_backlog_intake_doorbell(payload):
        wake = run_intake_wake(issue_id=issue_id, wake_source="doorbell")
        return {"decision": "doorbell", "issue": issue_id, **wake}

    row = state.get_outbox_event_by_source("linear_observation", issue_id)
    if not _is_needs_mj_context(payload, row):
        return {"decision": "ignored", "issue": issue_id, "reason": "not Needs-MJ"}

    current = _norm(_current_state(payload))
    event_type = _norm(payload.get("type"))
    note = _latest_comment_body(payload)
    if event_type == "comment" and _is_self_comment(note):
        return {"decision": "ignored", "issue": issue_id, "reason": "self comment"}
    comment_decision = _decision_from_comment(note) if event_type == "comment" else ""
    title = _issue_title(payload)

    if row is None and (comment_decision in {_APPROVED, _REJECTED} or current in {_APPROVED, _REJECTED, _NOT_APPROVED}):
        return {"decision": "ignored", "issue": issue_id, "reason": "no outbox row"}

    if comment_decision == _QUESTION or (event_type == "comment" and not comment_decision) or current == _QUESTION:
        add_comment(issue_id, _question_reply(issue_id, title, note))
        return {"decision": "question", "issue": issue_id, "commented": True}

    if row is None:
        return {"decision": "ignored", "issue": issue_id, "reason": "no outbox row"}

    if comment_decision == _APPROVED or current == _APPROVED:
        _update_outbox_payload_and_state(state, row, new_state="queued", note=note)
        add_comment(
            issue_id,
            "Approved received — Team OS will continue this card with MJ notes attached. Phase 6 remains blocked until approval UX is proven.",
        )
        return {"decision": "approved", "issue": issue_id, "commented": True, "queued": True}

    if comment_decision == _REJECTED or current in {_REJECTED, _NOT_APPROVED}:
        reason = "Rejected by MJ"
        if note:
            reason = f"Rejected by MJ: {note}"
        _update_outbox_payload_and_state(state, row, new_state="failed", note=note, last_error=reason)
        add_comment(
            issue_id,
            "Rejected received — Team OS will not continue this card. MJ notes were captured for revise/archive handling.",
        )
        return {"decision": "rejected", "issue": issue_id, "commented": True, "queued": False}

    return {"decision": "ignored", "issue": issue_id, "reason": f"state {current or '(none)'} is not a Team OS decision lane"}


def add_linear_comment(issue_id: str, body: str) -> None:
    """Post a Linear comment using the local linear-agent helper."""
    import runpy

    helper = runpy.run_path(str(Path("~/.hermes/bin/linear-agent").expanduser()))
    gql = helper["gql"]
    issue = gql("query($id:String!){ issue(id:$id){ id } }", {"id": issue_id})["issue"]
    gql(
        "mutation($input:CommentCreateInput!){ commentCreate(input:$input){ success comment { id } } }",
        {"input": {"issueId": issue["id"], "body": body}},
    )


def handle_team_os_linear_webhook(payload: dict[str, Any], *, state_db: str | Path | None = None) -> dict[str, Any]:
    db_path = Path(state_db).expanduser() if state_db else Path("~/.hermes/state/team-os/state.db").expanduser()
    return handle_linear_webhook(payload, state=TeamOSState(db_path), add_comment=add_linear_comment)
