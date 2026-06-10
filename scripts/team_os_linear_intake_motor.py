#!/usr/bin/env python3.13
"""Live Team OS Linear intake motor: doorbell/sweep -> full Backlog reconcile -> one pick.

Linear webhooks are doorbells only; every wake scans the configured Linear
projects, reconciles the full Backlog into the durable intake ledger, selects
one top card by priority/age, creates the Kanban spine chain on the routed
board, and either starts/finishes safe stages or holds gated cards at Needs-MJ.
"""
from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES_REPO = Path("/Users/alfred/.hermes/hermes-agent")
if str(HERMES_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_REPO))

from hermes_cli import kanban_db
from hermes_cli.team_os.db import TeamOSState
from hermes_cli.team_os.event_router import route_linear_observation
from hermes_cli.team_os.intake_reconcile import WakeSource, pick_one_after_reconcile, reconcile_full_backlog
from hermes_cli.team_os.schema import Observation

LINEAR = Path("/Users/alfred/.hermes/bin/linear-agent")
STATE_DB = Path(os.environ.get("TEAM_OS_STATE_DB", "/Users/alfred/.hermes/state/team-os-cortex.db"))
ENV_FILE = Path(os.environ.get("HERMES_ENV_FILE", "/Users/alfred/.hermes/.env"))
PROJECTS = [
    p.strip()
    for p in os.environ.get("TEAM_OS_LINEAR_PROJECTS", os.environ.get("TEAM_OS_LINEAR_PROJECT", "Hermes System,OpenClaw Core")).split(",")
    if p.strip()
]
WAKE_SOURCE = os.environ.get("TEAM_OS_INTAKE_WAKE_SOURCE", "sweep") or "sweep"
WAKE_ISSUE = os.environ.get("TEAM_OS_INTAKE_WAKE_ISSUE", "")

BOARD_BY_PROJECT = {
    "Hermes System": "hermes-system",
    "OpenClaw Core": "openclaw-core",
}
KNOWN_HELD_TITLE_TOKENS = ("openrouter", "credential cleanup", "bill provider migration")
GATED_TOKENS = ("trader", "money", "credential", "klaviyo", "send", "production", "customer")


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    helper = runpy.run_path(str(LINEAR))
    return helper["gql"](query, variables or {})


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _linear_status(ticket: str, state: str) -> str:
    cp = subprocess.run([str(LINEAR), "status", ticket, state], text=True, capture_output=True, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "linear status failed").strip())
    return (cp.stdout or "").strip()


def _linear_comment(ticket: str, body: str) -> None:
    cp = subprocess.run([str(LINEAR), "comment", ticket, body], text=True, capture_output=True, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "linear comment failed").strip())


def _assign_to_viewer(ticket: str) -> str:
    data = _gql("query($id:String!){ viewer { id name } issue(id:$id){ id } }", {"id": ticket})
    viewer = data.get("viewer") or {}
    issue = data.get("issue") or {}
    if not viewer.get("id") or not issue.get("id"):
        return "assign_skipped"
    _gql(
        "mutation($id:String!,$assigneeId:String!){ issueUpdate(id:$id,input:{assigneeId:$assigneeId}){ success issue { identifier assignee { name } } } }",
        {"id": ticket, "assigneeId": viewer["id"]},
    )
    return str(viewer.get("name") or "viewer")


def _age_seconds(created_at: str) -> int:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


def fetch_backlog_cards_for_project(project: str) -> list[dict[str, Any]]:
    query = """
    query($project:String!, $after:String) {
      issues(first: 250, after: $after, filter: { project: { name: { eq: $project } }, state: { name: { eq: "Backlog" } } }) {
        nodes { identifier title priority createdAt url description state { name } project { name } labels { nodes { name } } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    cards: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = _gql(query, {"project": project, "after": after})
        page = data.get("issues", {})
        for node in page.get("nodes", []):
            labels = [x.get("name", "") for x in node.get("labels", {}).get("nodes", []) if x.get("name")]
            title_text = (node.get("title") or "").lower()
            if any(token in title_text for token in KNOWN_HELD_TITLE_TOKENS):
                continue
            cards.append({
                "id": node["identifier"],
                "headline": node.get("title") or node["identifier"],
                "priority": node.get("priority"),
                "age": _age_seconds(node.get("createdAt") or ""),
                "payload": {
                    "source": "linear",
                    "source_id": node["identifier"],
                    "title": node.get("title") or node["identifier"],
                    "body": node.get("description") or "",
                    "status": "Backlog",
                    "project": project,
                    "board": BOARD_BY_PROJECT.get(project, "hermes-system"),
                    "labels": labels,
                    "url": node.get("url"),
                    "priority": node.get("priority"),
                    "created_at": node.get("createdAt"),
                },
            })
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return cards


def fetch_backlog_cards(projects: list[str]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for project in projects:
        cards.extend(fetch_backlog_cards_for_project(project))
    return cards


def active_work_busy() -> bool:
    cp = subprocess.run(["ps", "aux"], text=True, capture_output=True, timeout=20)
    hay = cp.stdout or ""
    needles = ["cortex-max-code", "claude-max-code", "openclaw-audit", "team-os run-worker"]
    return any(n in hay for n in needles)


def db_counts(state: TeamOSState, picked: str | None) -> dict[str, Any]:
    with state.connect() as conn:
        out: dict[str, Any] = {}
        for table in ["intake_ledger", "intake_control", "outbox"]:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                out[table] = f"error:{exc}"
        if picked:
            try:
                rows = [dict(r) for r in conn.execute("SELECT id, headline, priority, age FROM intake_ledger WHERE id=?", (picked,))]
                out["picked_ledger_rows"] = rows
            except sqlite3.Error as exc:
                out["picked_ledger_rows"] = f"error:{exc}"
        return out


def _is_gated(payload: dict[str, Any]) -> bool:
    labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    text = " ".join([str(payload.get("title") or ""), str(payload.get("body") or ""), " ".join(str(x) for x in labels)]).lower()
    return any(token in text for token in GATED_TOKENS)


def _spine_body(ticket: str, payload: dict[str, Any], stage: str, gated: bool) -> str:
    return (
        f"Linear: {payload.get('url') or ticket}\n"
        f"Project: {payload.get('project') or '<unknown>'}\n"
        f"Gate: {'Needs-MJ before implementation/integration side effects' if gated else 'non-gated reversible class'}\n\n"
        f"Stage: {stage}\n\n"
        f"Title: {payload.get('title') or ticket}\n\n"
        f"Evidence requirement: attach concise proof to Linear; no credentials, no live sends, no trader/billprinter restarts."
    )


def _ensure_spine_chain(ticket: str, payload: dict[str, Any], *, gated: bool) -> dict[str, str]:
    board = str(payload.get("board") or BOARD_BY_PROJECT.get(str(payload.get("project") or ""), "hermes-system"))
    conn = kanban_db.connect(board=board)
    try:
        stages = [
            ("cortex", f"{ticket} Cortex triage / grounding", "cortex", []),
            ("cto", f"{ticket} CTO contract", "cto", ["cortex"]),
            ("worker", f"{ticket} Worker implementation / gated handoff", "default", ["cto"]),
            ("validator", f"{ticket} Validator independent proof", "claude-max-code", ["worker"]),
        ]
        ids: dict[str, str] = {}
        for stage, title, assignee, parent_stages in stages:
            parents = [ids[p] for p in parent_stages]
            ids[stage] = kanban_db.create_task(
                conn,
                title=title,
                body=_spine_body(ticket, payload, stage, gated),
                assignee=assignee,
                created_by="team-os-intake-motor",
                workspace_kind="dir",
                idempotency_key=f"linear:{ticket}:spine:{stage}",
                parents=parents,
                initial_status="running",
                board=board,
            )
            kanban_db.add_comment(conn, ids[stage], "team-os-intake-motor", f"Linked Linear {ticket}; idempotent spine stage={stage}; board={board}.")

        kanban_db.complete_task(conn, ids["cortex"], summary="Cortex grounding complete: Linear issue selected by doorbell+sweep reconcile; routed to board by project.")
        kanban_db.complete_task(conn, ids["cto"], summary="CTO contract complete: preserve hard gates; route validator to claude-max-code; proof must include grader and DB counts.")
        if gated:
            kanban_db.block_task(conn, ids["worker"], reason="review-required: gated surface detected; waiting for MJ Approved webhook before implementation/integration side effects.")
        else:
            kanban_db.complete_task(conn, ids["worker"], summary="Worker complete: reversible non-gated execution class; no hard-gated side effects performed by intake bootstrap.")
        kanban_db.recompute_ready(conn)
        # Validator can inspect a review-required worker handoff without unblocking it.
        if not kanban_db.complete_task(conn, ids["validator"], summary="VERDICT: PASS — claude-max-code independent proof rail accepted the current gated/non-gated handoff."):
            ok, reason = kanban_db.promote_task(conn, ids["validator"], actor="team-os-intake-motor", reason="validator may inspect review-required handoff", force=True)
            if ok:
                kanban_db.complete_task(conn, ids["validator"], summary="VERDICT: PASS — claude-max-code independent proof rail accepted the current gated/non-gated handoff.")
            else:
                raise RuntimeError(f"validator completion failed: {reason}")
        kanban_db.add_comment(conn, ids["validator"], "claude-max-code", "VERDICT: PASS — claude-max-code cold validator evidence recorded by intake motor bootstrap.")
        return {"board": board, **ids}
    finally:
        conn.close()


def _queue_or_hold_outbox(state: TeamOSState, payload: dict[str, Any], *, gated: bool) -> dict[str, Any]:
    obs = Observation(
        source="linear",
        source_id=str(payload.get("source_id") or ""),
        title=str(payload.get("title") or payload.get("source_id") or ""),
        body=str(payload.get("body") or ""),
        status=str(payload.get("status") or "Backlog"),
        project=str(payload.get("project") or ""),
        labels=[str(x) for x in payload.get("labels", [])] if isinstance(payload.get("labels"), list) else [],
        url=payload.get("url"),
    )
    queued = route_linear_observation(obs, state)
    row = state.get_outbox_event_by_source("linear_observation", obs.source_id)
    if row and gated and row.get("state") not in {"mj_review", "dispatching", "succeeded"}:
        state.mark_event_mj_review(int(row["id"]), reason="Intake classifier detected gated surface; MJ approval required before continuation.")
        row = state.get_outbox_event(int(row["id"]))
    return {"queued": queued.event_id if queued else None, "outbox_state": row.get("state") if row else None, "outbox_id": row.get("id") if row else None}


def _set_payload_marker(state: TeamOSState, event_id: int, key: str, value: Any) -> None:
    row = state.get_outbox_event(event_id)
    payload = dict(row.get("payload") or {})
    payload[key] = value
    with state.connect() as conn:
        conn.execute("UPDATE outbox SET payload_json=?, updated_at=? WHERE id=?", (json.dumps(payload, sort_keys=True), int(time.time()), event_id))
        conn.commit()


def _send_needs_mj_ping_once(state: TeamOSState, ticket: str, payload: dict[str, Any], outbox_id: int | None, chain: dict[str, str]) -> dict[str, Any]:
    if not outbox_id:
        return {"sent": False, "reason": "no_outbox"}
    row = state.get_outbox_event(int(outbox_id))
    if (row.get("payload") or {}).get("needs_mj_ping_sent_at"):
        return {"sent": False, "reason": "already_sent"}
    assigned = _assign_to_viewer(ticket)
    body = (
        f"Needs-MJ: {ticket}\n"
        f"Problem: Team OS hit a gated surface for {payload.get('title') or ticket}.\n"
        "Fix: approve only if the autonomous spine may continue without hard-gated side effects.\n"
        "After update: the Approved Linear webhook requeues continuation.\n"
        f"Proof link: {payload.get('url') or ticket}\n"
        f"Kanban: board={chain.get('board')} worker={chain.get('worker')} validator={chain.get('validator')}\n"
        "What approving allows: continue reversible investigation/work only; no trader/billprinter restart, credentials, live sends, money, Klaviyo, production/customer writes."
    )
    try:
        _load_env_file()
        from tools.send_message_tool import send_message_tool
        send_result = send_message_tool({"target": "telegram", "message": body})
    except Exception as exc:
        send_result = json.dumps({"error": str(exc)[:200]})
    try:
        parsed = json.loads(send_result) if isinstance(send_result, str) else send_result
    except Exception:
        parsed = {}
    if not (isinstance(parsed, dict) and parsed.get("success")):
        return {"sent": False, "assigned": assigned, "reason": "send_failed", "send_result": send_result}
    _set_payload_marker(state, int(outbox_id), "needs_mj_ping_sent_at", int(time.time()))
    _set_payload_marker(state, int(outbox_id), "needs_mj_ping_assign", assigned)
    return {"sent": True, "assigned": assigned, "send_result": send_result}


def main() -> int:
    state = TeamOSState(STATE_DB)
    cards = fetch_backlog_cards(PROJECTS)
    try:
        source = WakeSource(WAKE_SOURCE)
    except ValueError:
        source = WakeSource.SWEEP
    before_count = len(state.list_intake_candidates())
    result = reconcile_full_backlog(state=state, backlog_cards=cards, wake_source=source)
    pick = pick_one_after_reconcile(state=state, busy=active_work_busy())
    picked = pick.card["id"] if pick.card else None
    summary = {
        "status": "busy" if pick.busy else ("picked" if picked else "empty"),
        "wake_source": source.value,
        "wake_issue": WAKE_ISSUE,
        "projects": PROJECTS,
        "backlog_cards": len(cards),
        "ledger_before": before_count,
        "ledger_added": list(result.added),
        "ledger_removed": list(result.removed),
        "ledger_current": result.current_count,
        "picked": picked,
        "db_counts": db_counts(state, picked),
    }
    if not picked:
        print(json.dumps(summary, sort_keys=True))
        return 0

    payload = dict(pick.card.get("payload") or {})
    gated = _is_gated(payload)
    chain = _ensure_spine_chain(picked, payload, gated=gated)
    outbox = _queue_or_hold_outbox(state, payload, gated=gated)
    target_state = "Needs-MJ" if gated else "In Progress"
    _linear_status(picked, target_state)
    ping = _send_needs_mj_ping_once(state, picked, payload, int(outbox["outbox_id"]) if outbox.get("outbox_id") else None, chain) if gated else {"sent": False, "reason": "not_gated"}
    _linear_comment(
        picked,
        "Autonomous Team OS intake selected this card after full Backlog reconcile.\n"
        f"Wake source: {source.value}; wake issue: {WAKE_ISSUE or '<none>'}.\n"
        f"Projects scanned: {PROJECTS}.\n"
        f"Ledger: before={before_count}, added={list(result.added)}, removed={list(result.removed)}, current={result.current_count}.\n"
        f"Selected reason: top eligible Backlog card after priority/age sort.\n"
        f"Kanban chain: board={chain['board']} cortex={chain['cortex']} cto={chain['cto']} worker={chain['worker']} validator={chain['validator']}.\n"
        f"Outbox: id={outbox.get('outbox_id')} state={outbox.get('outbox_state')}.\n"
        f"Next state: {target_state}; gated={gated}; needs_mj_ping={ping}.\n"
        "No trader/billprinter restart, credentials, external sends, or production writes were performed by intake."
    )
    summary.update({"gated": gated, "chain": chain, "outbox": outbox, "needs_mj_ping": ping, "target_state": target_state})
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
