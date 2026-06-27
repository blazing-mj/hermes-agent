"""Event-driven Linear approval webhook handling for Team OS.

This module is intentionally deterministic and small: Linear webhooks are the
wake-up signal, not a polling loop. It handles only the MJ decision lanes and
Needs-MJ comments, then records the result in the Team OS outbox and Linear.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .. import kanban_db

from .db import TeamOSState

AddComment = Callable[[str, str], None]
RunIntakeWake = Callable[..., dict[str, Any]]
RunIntegratorAutoLand = Callable[..., dict[str, Any]]

BOARD_BY_PROJECT = {
    "Hermes System": "hermes-system",
    "OpenClaw Core": "openclaw-core",
}

DEFAULT_TEAM_OS_STATE_DB = "/Users/alfred/.hermes/state/team-os-cortex.db"

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
    extra_payload: dict[str, Any] | None = None,
) -> None:
    payload = dict(row.get("payload") or {})
    if note:
        payload["mj_notes"] = note
    if extra_payload:
        payload.update(extra_payload)
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


def apply_mj_decision(
    state: TeamOSState,
    row: dict[str, Any],
    *,
    decision: str,
    note: str = "",
) -> str:
    """Apply an MJ decision to a pending outbox row.

    The webhook and sweep fallback share this transition so a missed Linear
    delivery cannot leave a card stranded in ``mj_review``.
    """

    if row.get("state") != "mj_review":
        return str(row.get("state") or "unchanged")

    normalized = _norm(decision)
    if normalized == _APPROVED:
        _update_outbox_payload_and_state(state, row, new_state="queued", note=note)
        return "queued"
    if normalized in {_REJECTED, _NOT_APPROVED}:
        reason = "Rejected by MJ"
        if note:
            reason = f"Rejected by MJ: {note}"
        _update_outbox_payload_and_state(state, row, new_state="failed", note=note, last_error=reason)
        return "failed"
    raise ValueError(f"Unsupported MJ decision: {decision}")


def _kanban_project_from_row(row: dict[str, Any] | None) -> str:
    payload = row.get("payload") if row else {}
    if isinstance(payload, dict):
        return str(payload.get("project") or "")
    return ""


def unblock_approved_kanban_worker(ticket: str, project: str) -> dict[str, Any]:
    board = BOARD_BY_PROJECT.get(project, "hermes-system")
    conn = kanban_db.connect(board=board)
    try:
        row = conn.execute(
            """
            SELECT id, status FROM tasks
             WHERE idempotency_key = ? AND status != 'archived'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (f"linear:{ticket}:spine:worker",),
        ).fetchone()
        if not row:
            return {"board": board, "worker": "", "unblocked": False, "reason": "worker task not found"}
        worker_id = str(row["id"])
        unblocked = kanban_db.unblock_task(conn, worker_id)
        if unblocked:
            kanban_db.add_comment(
                conn,
                worker_id,
                "team-os-linear-webhook",
                "Linear Approved received; cleared the Needs-MJ block so the worker can continue.",
            )
            kanban_db.recompute_ready(conn)
            return {"board": board, "worker": worker_id, "unblocked": True}
        return {"board": board, "worker": worker_id, "unblocked": False, "reason": f"worker status is {row['status']}"}
    finally:
        conn.close()


def _send_integrator_fyi(ticket: str, body: str) -> dict[str, Any]:
    try:
        from tools.send_message_tool import send_message_tool

        raw = send_message_tool({"target": "telegram", "message": body})
    except Exception as exc:  # noqa: BLE001 - FYI failure must be visible but not hide rollback/proof.
        return {"sent": False, "reason": str(exc)[:200]}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        parsed = {"raw": raw}
    if isinstance(parsed, dict) and parsed.get("success"):
        message_id = parsed.get("message_id") or parsed.get("id") or parsed.get("result")
        return {"sent": True, "message_id": message_id, "raw": parsed}
    return {"sent": False, "raw": parsed}


_REPOS = ("/Users/alfred/.hermes/hermes-agent", "/Users/alfred/.openclaw")
_NO_CODE_RE = re.compile(r"no-?code|investigation-?only|docs-?only|observation-?only|no code changes", re.I)


def _landing_evidence(conn: Any, ticket: str) -> dict[str, Any]:
    """AGENTS-244 gate: real commit sha verifiable in a local repo, or an explicit
    no-code declaration, gathered from the chain's task bodies/comments."""
    import re as _re
    import subprocess as _sp
    text_parts: list[str] = []
    try:
        rows = conn.execute(
            "SELECT t.body, tc.body AS cbody FROM tasks t LEFT JOIN task_comments tc ON tc.task_id = t.id "
            "WHERE t.idempotency_key LIKE ?", (f"linear:{ticket}:spine:%",),
        ).fetchall()
        for r in rows:
            text_parts.append(str(r["body"] or ""))
            text_parts.append(str(r["cbody"] or ""))
    except Exception:
        pass
    text = " ".join(text_parts)
    if _NO_CODE_RE.search(text):
        return {"ok": True, "kind": "no-code-declared"}
    for sha in set(_re.findall(r"\b[0-9a-f]{9,40}\b", text)):
        for repo in _REPOS:
            try:
                p = _sp.run(["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"],
                            capture_output=True, timeout=10)
                if p.returncode == 0:
                    return {"ok": True, "kind": "commit", "sha": sha, "repo": repo}
            except Exception:
                continue
    return {"ok": False}


_RESTRICTED_WRITER = "/Users/alfred/.hermes/hermes-agent/scripts/restricted_linear_writer.py"


def _compose_landed_summary(ticket: str, evidence: dict[str, Any], notes: str) -> str:
    """Human-language landed summary (GATE-CARD-TEMPLATE tone): what changed,
    where, and the rollback ref — readable by MJ without opening code."""
    if evidence.get("kind") == "commit":
        repo = Path(str(evidence.get("repo", ""))).name or "repo"
        sha = str(evidence.get("sha", ""))[:9]
        what = (
            f"Landed: the approved change is committed in `{repo}` at `{sha}`.\n"
            f"Rollback: `git -C {evidence.get('repo')} revert {sha}` restores the previous behavior."
        )
    else:
        what = (
            "Landed: investigation/no-code outcome — findings are recorded in this ticket's chain; "
            "no code or config changed, so there is nothing to roll back."
        )
    notes_line = f"\nMJ constraints carried forward: {notes}" if notes else ""
    return (
        f"{what}\n"
        "Validator: independent PASS. Gate: MJ approved on Linear; the Integrator completed the final "
        "hop automatically (AGENTS-238). No trader/billprinter restart, credentials, live sends, money, "
        f"or production/customer writes were performed.{notes_line}"
    )


def _integrator_finalize_linear(ticket: str, evidence: dict[str, Any], notes: str) -> dict[str, Any]:
    """AGENTS-238 final hop: landed-summary comment + Approved→Done through the
    gated restricted writer (allowlist tuple: Approved→Done by integrator).

    Failure here must never un-land the run — the kanban chain already carries
    the rollback story; the caller records this outcome either way.
    """
    import sys as _sys

    proposal = {
        "actions": [
            {
                "action": "comment",
                "issue": ticket,
                "body": _compose_landed_summary(ticket, evidence, notes),
            },
            {
                "action": "status",
                "issue": ticket,
                "from": "Approved",
                "to": "Done",
                "by": "integrator",
                "conditions": [
                    "mj_approved",
                    "validator_pass_independent",
                    "landing_evidence_present",
                ],
            },
        ]
    }
    try:
        proc = subprocess.run(
            [_sys.executable, _RESTRICTED_WRITER],
            input=json.dumps(proposal),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "done_moved": proc.returncode == 0,
            "writer_rc": proc.returncode,
            "writer_out": (proc.stdout or proc.stderr or "")[:400],
        }
    except Exception as exc:  # noqa: BLE001 - finalize failure must not unland the run
        return {"done_moved": False, "reason": str(exc)[:200]}


def run_integrator_auto_land(ticket: str, project: str, notes: str = "") -> dict[str, Any]:
    """Deterministically land the approved reversible Team OS continuation.

    This is intentionally narrower than git/deploy auto-land: for the human-gate
    continuation proof, Integrator lands the reversible local Team OS state
    transition only — worker completion, Integrator audit task, Linear comment,
    and training-wheel FYI ping.  It never restarts trader/billprinter, edits
    credentials, sends customer mail, or touches money/trading surfaces.
    """

    board = BOARD_BY_PROJECT.get(project, "hermes-system")
    conn = kanban_db.connect(board=board)
    try:
        worker = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (f"linear:{ticket}:spine:worker",),
        ).fetchone()
        validator = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (f"linear:{ticket}:spine:validator",),
        ).fetchone()
        if not worker:
            return {"status": "did_not_fire", "board": board, "reason": "worker task not found"}
        if not validator or validator["status"] != "done":
            return {"status": "needs_mj", "board": board, "worker": worker["id"], "reason": "validator PASS task not done"}

        # AGENTS-244: NO EMPTY LANDINGS. A landing requires either a real commit
        # (sha in the chain's comments, verifiable in a local repo) or an explicit
        # investigation-only/no-code declaration. Otherwise BOUNCE to the worker.
        evidence = _landing_evidence(conn, ticket)
        if not evidence["ok"]:
            return {
                "status": "bounced_empty_landing", "board": board, "worker": str(worker["id"]),
                "reason": "no landed commit and no no-code declaration in chain — refusing ceremony landing",
            }

        worker_id = str(worker["id"])
        if worker["status"] != "done":
            if not kanban_db.complete_task(
                conn,
                worker_id,
                summary="Approved continuation landed: reversible investigation/work may proceed; no trader/billprinter restart, credentials, live sends, money, or production/customer writes performed.",
                metadata={"linear_ticket": ticket, "approved_notes": notes, "integrator": "auto_land"},
            ):
                return {"status": "did_not_fire", "board": board, "worker": worker_id, "reason": f"worker status is {worker['status']}"}

        integrator_id = kanban_db.create_task(
            conn,
            title=f"{ticket} Integrator auto-land / reversible continuation",
            body=(
                f"Linear: {ticket}\nProject: {project}\n"
                "Scope: reversible Team OS continuation marker only. No trader/billprinter restart, credentials, live sends, money, or production/customer writes.\n"
                f"MJ notes: {notes or '<none>'}"
            ),
            assignee="team-os",  # control-plane marker — non-profile so the kanban dispatcher skips it
            created_by="team-os-integrator",
            workspace_kind="dir",
            idempotency_key=f"linear:{ticket}:spine:integrator",
            parents=[worker_id, str(validator["id"])],
            initial_status="running",
            board=board,
        )
        kanban_db.add_comment(
            conn,
            integrator_id,
            "team-os-integrator",
            "Integrator auto-landed reversible continuation after Linear Approved. Rollback: move card back to Needs-MJ and reopen worker block; no external side effects were performed.",
        )
        kanban_db.complete_task(
            conn,
            integrator_id,
            summary="Integrator auto_landed reversible continuation; FYI ping attempted; rollback is recorded in this comment.",
            metadata={"linear_ticket": ticket, "reversibility": "reversible", "fyi": "training-wheel"},
        )
        fyi = _send_integrator_fyi(
            ticket,
            f"FYI: {ticket} Integrator auto-landed the reversible Team OS continuation after your Linear Approved decision. No trader/billprinter restart, credentials, live sends, money, or production/customer writes were performed. Rollback is recorded on Linear/Kanban.",
        )
        # AGENTS-238 final hop: ticket → Done + human-language landed-summary.
        finalize = _integrator_finalize_linear(ticket, evidence, notes)
        return {"status": "auto_landed", "board": board, "worker": worker_id, "integrator": integrator_id, "fyi_sent": bool(fyi.get("sent")), "fyi": fyi, "linear_finalize": finalize}
    finally:
        conn.close()


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


def _kill_switch_path() -> Path:
    """Global Team OS kill-switch file. Honors HERMES_HOME (so test isolation
    and profile homes resolve correctly), defaulting to ~/.hermes — the root
    home the main gateway that serves this webhook runs under."""
    home = os.environ.get("HERMES_HOME", "").strip() or "~/.hermes"
    return Path(home).expanduser() / "state" / "team-os-kill-switch.json"


def _team_os_paused() -> bool:
    """Team OS hard-pause gate. ``KillSwitch`` fails CLOSED on a corrupt/
    unreadable state file (treats it as enabled), and this wrapper also returns
    True on any unexpected error — so the webhook errs toward NOT landing when
    the pause state can't be determined. Pending decisions are recovered by the
    intake sweep once the pause is lifted, so nothing is lost."""
    try:
        from .kill_switch import KillSwitch
        return KillSwitch(_kill_switch_path()).is_enabled()
    except Exception:
        return True


def handle_linear_webhook(
    payload: dict[str, Any],
    *,
    state: TeamOSState,
    add_comment: AddComment,
    run_intake_wake: RunIntakeWake = run_cortex_intake_wake,
    run_integrator_auto_land: RunIntegratorAutoLand | None = None,
) -> dict[str, Any]:
    """Handle one Linear webhook payload for Team OS approval UX.

    Supported events:
    - Issue moved from Needs-MJ to Approved/Rejected/Question.
    - Comment created on a Needs-MJ issue.

    Low-cost or unrelated cards are ignored: no assignment, no ping, no comment.
    When the Team OS kill-switch is enabled this is a HARD pause: nothing is
    processed (no doorbell intake, no approval landing) — pending Approved/
    Rejected cards are reconciled by the intake sweep when the pause lifts.
    """
    issue_id = _issue_identifier(payload)
    if not issue_id:
        return {"decision": "ignored", "reason": "missing issue identifier"}

    if _team_os_paused():
        return {"decision": "ignored", "reason": "team-os paused (kill-switch)", "issue": issue_id}

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

    if row.get("state") != "mj_review" and (
        comment_decision in {_APPROVED, _REJECTED} or current in {_APPROVED, _REJECTED, _NOT_APPROVED}
    ):
        return {"decision": "duplicate", "issue": issue_id, "outbox_state": row.get("state")}

    if comment_decision == _APPROVED or current == _APPROVED:
        apply_mj_decision(state, row, decision=_APPROVED, note=note)
        kanban_result = unblock_approved_kanban_worker(issue_id, _kanban_project_from_row(row))
        # WIRE (Stage E hook, dormant): MJ approved a gated ticket → run the real
        # Worker→Validator on its contract BEFORE the Integrator lands (so there's
        # a real commit for the no-empty-landing gate). MJ's approval satisfies
        # the human gate, so clear it for this run. execute_spine self-gates on
        # TEAM_OS_WORKER_DISPATCH/VALIDATOR_DISPATCH (off → no-op), so this is
        # byte-for-byte unchanged until turn-on.
        execution: dict[str, Any] = {"ran": False, "reason": "no contract"}
        try:
            from .worker_dispatch import execute_spine
            _decoded = state.get_outbox_event(int(row["id"]))
            _contract = dict((_decoded.get("payload") or {}).get("cto_contract") or {})
            if _contract:
                _contract["human_gate_required"] = False  # MJ's approval satisfied the gate
                execution = execute_spine(_contract)
        except Exception as exc:  # noqa: BLE001 - dormant wire must never break approval
            execution = {"ran": False, "reason": f"execute_spine error: {str(exc)[:160]}"}
        wake = run_intake_wake(issue_id=issue_id, wake_source="completion")
        integrator_runner = run_integrator_auto_land or globals()["run_integrator_auto_land"]
        integrator_result = integrator_runner(ticket=issue_id, project=_kanban_project_from_row(row), notes=note)
        refreshed = state.get_outbox_event(int(row["id"]))
        final_state = "succeeded" if integrator_result.get("status") == "auto_landed" else str(refreshed.get("state") or "queued")
        _update_outbox_payload_and_state(
            state,
            refreshed,
            new_state=final_state,
            note=note,
            extra_payload={"integrator_auto_land": integrator_result},
        )
        add_comment(
            issue_id,
            f"Approved received — Team OS queued continuation with MJ notes attached, cleared the Kanban worker block ({kanban_result}), woke the intake/dispatch motor, and ran Integrator ({integrator_result}).",
        )
        return {"decision": "approved", "issue": issue_id, "commented": True, "queued": True, "kanban": kanban_result, "integrator": integrator_result, "execution": execution, **wake}

    if comment_decision == _REJECTED or current in {_REJECTED, _NOT_APPROVED}:
        apply_mj_decision(state, row, decision=_REJECTED, note=note)
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
    db_path = Path(state_db or os.environ.get("TEAM_OS_STATE_DB") or DEFAULT_TEAM_OS_STATE_DB).expanduser()
    return handle_linear_webhook(payload, state=TeamOSState(db_path), add_comment=add_linear_comment)
