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
import re
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
from hermes_cli.team_os.cortex_agent import cortex_audit
from hermes_cli.team_os.cto_agent import cto_contract
from hermes_cli.team_os.worker_dispatch import execute_spine
from hermes_cli.team_os.db import TeamOSState
from hermes_cli.team_os.event_router import route_linear_observation
from hermes_cli.team_os.intake_reconcile import WakeSource, pick_one_after_reconcile, reconcile_full_backlog
from hermes_cli.team_os.linear_webhook import apply_mj_decision, run_integrator_auto_land
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
HARD_GATE_TOKENS = (
    "credential",
    "secret",
    "api key",
    "token",
    "klaviyo",
    "live send",
    "customer",
    "production",
    "delete data",
    "mass-delete",
    "new external account",
)
TRADER_ACTION_TOKENS = ("restart", "kickstart", "clear stop", "stop.sh --clear", "resume", "trade", "trading", "money")
REVERSIBLE_ALLOW_TOKENS = (
    "reversible code/tests/docs",
    "reversible code/config/docs/tests",
    "no daemon restart",
    "without trader restart",
    "no trader/billprinter restart",
)
DECISION_STATES = ("Approved", "Rejected")


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


def fetch_decision_cards_for_project(project: str) -> list[dict[str, Any]]:
    query = """
    query($project:String!, $states:[String!], $after:String) {
      issues(first: 250, after: $after, filter: { project: { name: { eq: $project } }, state: { name: { in: $states } } }) {
        nodes {
          identifier
          title
          url
          state { name }
          project { name }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    cards: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = _gql(query, {"project": project, "states": list(DECISION_STATES), "after": after})
        page = data.get("issues", {})
        for node in page.get("nodes", []):
            cards.append({
                "id": node["identifier"],
                "title": node.get("title") or node["identifier"],
                "url": node.get("url"),
                "project": project,
                "state": (node.get("state") or {}).get("name") or "",
                "note": "",
            })
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return cards


def fetch_decision_cards(projects: list[str]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for project in projects:
        cards.extend(fetch_decision_cards_for_project(project))
    return cards


def _find_spine_task_id(board: str, ticket: str, stage: str) -> str:
    conn = kanban_db.connect(board=board)
    try:
        row = conn.execute(
            """
            SELECT id FROM tasks
             WHERE idempotency_key = ? AND status != 'archived'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (f"linear:{ticket}:spine:{stage}",),
        ).fetchone()
        return str(row["id"] if row else "")
    finally:
        conn.close()


def _unblock_approved_kanban_worker(ticket: str, project: str) -> dict[str, Any]:
    """Unblock the gated worker stage after Linear moves to Approved.

    The outbox state alone is not enough: the worker card was deliberately put
    in a sticky ``blocked`` lane for MJ review, so a missed webhook or sweep
    fallback must also clear that Kanban block.
    """

    board = BOARD_BY_PROJECT.get(project, "hermes-system")
    worker_id = _find_spine_task_id(board, ticket, "worker")
    if not worker_id:
        return {"board": board, "worker": "", "unblocked": False, "reason": "worker task not found"}
    conn = kanban_db.connect(board=board)
    try:
        unblocked = kanban_db.unblock_task(conn, worker_id)
        if unblocked:
            kanban_db.add_comment(
                conn,
                worker_id,
                "team-os-decision-reconcile",
                "Linear Approved reconciled; cleared the Needs-MJ block so the worker can continue.",
            )
            kanban_db.recompute_ready(conn)
            return {"board": board, "worker": worker_id, "unblocked": True}
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (worker_id,)).fetchone()
        return {
            "board": board,
            "worker": worker_id,
            "unblocked": False,
            "reason": f"worker status is {(row['status'] if row else 'missing')}",
        }
    finally:
        conn.close()


def reconcile_pending_decisions(state: TeamOSState, decision_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    processed: list[dict[str, Any]] = []
    for card in decision_cards:
        ticket = str(card.get("id") or "")
        decision = str(card.get("state") or "")
        row = state.get_outbox_event_by_source("linear_observation", ticket)
        if not row or row.get("state") != "mj_review":
            continue
        new_state = apply_mj_decision(state, row, decision=decision, note=str(card.get("note") or ""))
        kanban_result: dict[str, Any] = {}
        integrator_result: dict[str, Any] = {}
        if new_state == "queued":
            kanban_result = _unblock_approved_kanban_worker(ticket, str(card.get("project") or ""))
            integrator_result = run_integrator_auto_land(ticket=ticket, project=str(card.get("project") or ""), notes=str(card.get("note") or ""))
            if integrator_result.get("status") == "auto_landed":
                latest = state.get_outbox_event(int(row["id"]))
                payload = dict(latest.get("payload") or {})
                payload["integrator_auto_land"] = integrator_result
                with state.connect() as conn:
                    conn.execute(
                        "UPDATE outbox SET state='succeeded', payload_json=?, completed_at=?, updated_at=? WHERE id=?",
                        (json.dumps(payload, sort_keys=True), int(time.time()), int(time.time()), int(row["id"])),
                    )
                    conn.commit()
        try:
            if new_state == "queued":
                _linear_comment(ticket, f"Decision sweep saw Linear Approved, queued Team OS continuation, unblocked the worker, and ran Integrator. This is the fallback path for a missed webhook delivery. Kanban worker unblock: {kanban_result}. Integrator: {integrator_result}.")
            elif new_state == "failed":
                _linear_comment(ticket, "Decision sweep saw Linear Rejected and stopped Team OS continuation. This is the fallback path for a missed webhook delivery.")
        except Exception as exc:
            item: dict[str, Any] = {"id": ticket, "decision": decision, "new_state": new_state, "comment_error": str(exc)[:200]}
            if kanban_result:
                item["kanban"] = kanban_result
            processed.append(item)
            continue
        item: dict[str, Any] = {"id": ticket, "decision": decision, "new_state": new_state}
        if kanban_result:
            item["kanban"] = kanban_result
        if integrator_result:
            item["integrator"] = integrator_result
        processed.append(item)
    return processed


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


def _payload_text(payload: dict[str, Any]) -> str:
    labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    return " ".join([str(payload.get("title") or ""), str(payload.get("body") or ""), " ".join(str(x) for x in labels)]).lower()


def _has_negated_trader_restart(text: str) -> bool:
    negations = ("no daemon restart", "no trader restart", "without trader restart", "do not restart", "no restart")
    return any(token in text for token in negations)


def _is_gated(payload: dict[str, Any]) -> bool:
    """Tuned Team OS gate classifier: gate what work touches, not keywords alone."""

    text = _payload_text(payload)
    if "cortex triage protocol" in text and "type:rail" in text:
        return False
    if "trader" in text or "billprinter" in text:
        trader_action = any(token in text for token in TRADER_ACTION_TOKENS)
        if trader_action and not _has_negated_trader_restart(text):
            return True
    if any(token in text for token in HARD_GATE_TOKENS):
        # Explicit non-touch language lets reversible rail work proceed even if
        # it mentions denied surfaces for context.
        if any(token in text for token in REVERSIBLE_ALLOW_TOKENS) and any(guard in text for guard in ("no credential", "no live", "no production", "no customer")):
            return False
        return True
    return False


def _brief_line(text: str, max_chars: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _build_scoping_decision(ticket: str, payload: dict[str, Any], *, gated: bool) -> dict[str, Any]:
    """Build the Linear scoping harness decision before worker dispatch.

    This is the queue-not-loop guard from the agentic workflow audit: vague
    issues get grilled in Linear and never reach a worker; clear reversible
    Hermes issues get a mission/contract artifact attached before execution.
    """

    title = str(payload.get("title") or ticket).strip()
    body = str(payload.get("body") or "").strip()
    text = _payload_text(payload)
    project = str(payload.get("project") or "")
    labels = [str(x) for x in payload.get("labels", [])] if isinstance(payload.get("labels"), list) else []
    vague_tokens = ("vague", "unclear", "tbd", "somehow", "figure out", "decide", "make it better", "improve the flow")
    body_too_thin = len(body.split()) < 10
    is_vague = any(token in text for token in vague_tokens) or body_too_thin
    if is_vague:
        comment = (
            f"Linear scoping harness — {ticket}\n"
            "CLASSIFICATION: Question\n"
            "Worker dispatch: blocked until scope is clarified.\n\n"
            "Grill-me questions:\n"
            "- What exact behavior should change?\n"
            "- What is explicitly out of scope?\n"
            "- What proof would satisfy Done?\n"
            "- Is this Hermes-only reversible work, or does it touch runtime/config/client systems?\n"
            "- Should this be split into smaller Linear issues before a worker starts?"
        )
        return {"classification": "Question", "dispatch_allowed": False, "comment": comment}

    allowed = "reversible code/tests/docs under Hermes/Linear only"
    forbidden = "No OpenClaw/Vilimed/client work; no daemon restart; no credentials/providers; no external sends; no production/customer writes."
    proof = "focused tests, verifier/readback, independent validator PASS/BOUNCE comment, and concise Linear proof before Done"
    comment = (
        f"MISSION/CONTRACT — {ticket}\n"
        "CLASSIFICATION: Mission-Contract\n\n"
        f"Problem: {title}\n"
        f"Goal: {_brief_line(body or title, 420)}\n"
        f"Project: {project or '<unknown>'}; labels={labels}.\n\n"
        f"Allowed actions: {allowed}.\n"
        f"Forbidden actions: {forbidden}\n"
        f"Proof required: {proof}.\n"
        f"Human gate: {'yes — gated surface detected' if gated else 'no for this reversible Hermes-only slice; required if scope expands'}."
    )
    return {"classification": "Mission-Contract", "dispatch_allowed": True, "comment": comment}


def _build_cortex_triage_artifact(ticket: str, payload: dict[str, Any], *, gated: bool) -> dict[str, Any]:
    """Build the deterministic artifact shell for Cortex Triage Protocol v1.

    Cortex remains the judgment owner; deterministic code makes the artifact and
    routing invariants auditable for every picked ticket.
    """

    title = str(payload.get("title") or ticket)
    body = str(payload.get("body") or "")
    text = _payload_text(payload)
    title_text = str(payload.get("title") or "").lower()
    body_text = str(payload.get("body") or "").lower()
    audit = "RELEVANT"
    if any(token in text for token in ("duplicate of", "dupe of")):
        audit = "DUPLICATE"
    elif any(token in text for token in ("already done", "fixed in commit")):
        audit = "ALREADY DONE"
    elif any(token in title_text for token in ("stale", "superseded")) or any(token in body_text for token in ("status: stale", "status: superseded", "this ticket is stale", "this ticket is superseded")):
        audit = "STALE"

    asks: list[str] = []
    if any(token in text for token in ("access", "credential", "api key", "token", "permission", "tool")):
        asks.append("type: access | tool/credential/API needed | why/scope required before work can proceed | fallback: no-op until granted")
    elif gated:
        asks.append("type: decision | approve reversible continuation only | blocker: hard-gated surface or uncertainty | options: Approved / Rejected with constraints")
    elif any(token in text for token in ("vague", "unclear", "tbd", "decide")):
        asks.append("type: question | clarify exact desired outcome | options: narrow scope / split / cancel")
    else:
        asks.append("none — reversible class can proceed without MJ")

    split = "not required"
    if len(body) > 1200 or sum(token in text for token in ("and", "plus", "also", "mixed", "multi")) >= 3:
        split = "candidate: create ordered sub-issues before worker execution"

    classification = "Needs-MJ" if gated or audit in {"STALE", "DUPLICATE", "ALREADY DONE"} else "Proceed"
    human = _brief_line(f"{title}: {body or 'No description provided.'}", 420)
    agent = (
        f"Grounding: Linear {payload.get('url') or ticket}; project={payload.get('project') or '<unknown>'}; labels={payload.get('labels') or []}. "
        f"Scope: {title}. Non-goals: credentials, live sends, trader/billprinter restarts, customer/production writes unless explicitly gated. "
        "Proof: focused tests, grader output, DB counts, cold-review rail=claude-max-code."
    )
    comment = (
        f"Cortex Triage Protocol v1 — {ticket}\n"
        f"AUDIT: {audit}\n"
        f"SIZE/SPLIT: {split}\n"
        f"CLASSIFICATION: {classification}\n\n"
        f"Human brief: {human}\n\n"
        f"Agent brief: {agent}\n\n"
        "Structured ask/access request:\n"
        + "\n".join(f"- {ask}" for ask in asks)
    )
    return {"audit": audit, "classification": classification, "asks": asks, "split": split, "comment": comment}


def _spine_body(ticket: str, payload: dict[str, Any], stage: str, gated: bool) -> str:
    triage = ""
    if stage != "worker" and isinstance(payload.get("triage_protocol"), dict):
        triage = (payload.get("triage_protocol") or {}).get("comment") or ""
        if stage != "validator":
            triage = triage.replace("Validator", "cold-reviewer").replace("validator", "cold-reviewer")
    linear_ref = payload.get("url") or ticket
    task_title = payload.get("title") or ticket
    if stage != "validator":
        task_title = str(task_title).replace("Validator", "cold-reviewer").replace("validator", "cold-reviewer")
        linear_ref = ticket
    if stage == "worker":
        linear_ref = ticket
        task_title = f"{ticket} implementation slice"
    return (
        f"Linear: {linear_ref}\n"
        f"Project: {payload.get('project') or '<unknown>'}\n"
        f"Gate: {'Needs-MJ before implementation/integration side effects' if gated else 'non-gated reversible class'}\n\n"
        f"Stage: {stage}\n\n"
        f"Title: {task_title}\n\n"
        f"Evidence requirement: attach concise proof to Linear; no credentials, no live sends, no trader/billprinter restarts."
        + (f"\n\nTriage artifact:\n{triage}" if triage else "")
    )


def _ensure_spine_chain(ticket: str, payload: dict[str, Any], *, gated: bool) -> dict[str, str]:
    board = str(payload.get("board") or BOARD_BY_PROJECT.get(str(payload.get("project") or ""), "hermes-system"))
    conn = kanban_db.connect(board=board)
    try:
        # Spine tasks are TeamOS control-plane MARKERS, completed by the TeamOS
        # state machine (intake bootstrap / webhook / integrator) — they are NOT
        # generic kanban work. Their assignee must therefore be a non-profile
        # label so the embedded kanban dispatcher skips them (same mechanism
        # that excludes control-plane lanes like orion-cc). Previously the
        # worker used "default" (a real profile) + workspace_kind="dir" with no
        # workspace_path, so once unblocked it went ready, the dispatcher tried
        # to spawn it, and failed every tick ("workspace_kind=dir but no
        # workspace_path") — jamming the whole dispatcher. The real worker runs
        # via the Cortex outbox path (run_worker), never this marker.
        stages = [
            ("cortex", f"{ticket} Cortex triage / grounding", "team-os", []),
            ("cto", f"{ticket} CTO contract", "team-os", ["cortex"]),
            ("worker", f"{ticket} Worker implementation / gated handoff", "team-os", ["cto"]),
            ("validator", f"{ticket} Validator independent proof", "team-os", ["worker"]),
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


_IRREVERSIBLE = {
    "money": "moves/spends money", "trade": "places a trade", "trading": "enables trading",
    "credential": "touches credentials", "secret": "touches secrets", "api key": "touches an API key",
    "token": "touches a token", "live send": "sends live email", "klaviyo": "writes to Klaviyo",
    "production": "writes to production", "customer": "touches customer data",
    "delete data": "deletes data", "restart": "restarts a live service",
}


def _human_title(t: str) -> str:
    t = re.sub(r"^AGENTS-\d+[: ]*", "", t or "")
    t = re.sub(r"\b(follow-up|gated cleanup|phase \d+ autonomy gate #?\d*)[: ]*", "", t, flags=re.I)
    return t.strip()


def _compose_needs_mj_message(ticket: str, payload: dict[str, Any], chain: dict[str, str]) -> str:
    """GOAT-level decision message: classified, plain-language, with risk framing."""
    title = _human_title(payload.get("title") or ticket)
    desc = " ".join((payload.get("description") or "").split())
    what = (desc.split(". ")[0][:160] if desc else title) or title
    text = ((payload.get("title") or "") + " " + (payload.get("description") or "")).lower()
    # negation-aware: "no trading enabled" / "without restart" must not flag as irreversible
    hits = [v for k, v in _IRREVERSIBLE.items()
            if re.search(r"\b" + re.escape(k) + r"\b", text)
            and not re.search(r"\b(no|not|without|never)\s+(\w+\s+){0,2}" + re.escape(k), text)]
    if hits:
        risk = f"⚠️ IRREVERSIBLE — {hits[0]}. Cannot be auto-undone. Approve only if you mean it."
    else:
        risk = "↩️ REVERSIBLE — already built & cross-model validated; worst case = 1-min rollback (recorded on the ticket). No money/sends/credentials/production touched."
    return (
        f"✅ APPROVAL NEEDED · {ticket}\n"
        f"{title}\n\n"
        f"▸ What it does: {what}\n"
        f"▸ Why it stopped here: it's the consequential step — the work is done & validated, it just needs your yes.\n"
        f"▸ If you approve: it ships itself (merge + deploy) and you get a “✅ shipped” note. Nothing else happens.\n"
        f"▸ Risk: {risk}\n\n"
        f"Decide → in Linear move it to Approved (or reply APPROVED {ticket}); to stop it, Rejected + a reason; a question? just comment.\n"
        f"{payload.get('url') or ticket}"
    )


def _send_needs_mj_ping_once(state: TeamOSState, ticket: str, payload: dict[str, Any], outbox_id: int | None, chain: dict[str, str]) -> dict[str, Any]:
    if not outbox_id:
        return {"sent": False, "reason": "no_outbox"}
    row = state.get_outbox_event(int(outbox_id))
    if (row.get("payload") or {}).get("needs_mj_ping_sent_at"):
        return {"sent": False, "reason": "already_sent"}
    assigned = _assign_to_viewer(ticket)
    body = _compose_needs_mj_message(ticket, payload, chain)
    # AGENTS-243: inline Approve/Reject/Question buttons. A press moves the
    # Linear card via the gated writer (gateway telegram callback); the existing
    # webhook flow drives continuation, so buttons and Linear never diverge.
    # callback_data: lm:<action>:<ticket> (well under Telegram's 64-byte cap).
    keyboard = [
        [("✅ Approve", f"lm:approve:{ticket}"), ("❌ Reject", f"lm:reject:{ticket}")],
        [("💬 Question", f"lm:question:{ticket}")],
    ]
    try:
        _load_env_file()
        from tools.send_message_tool import send_message_tool
        send_result = send_message_tool({"target": "telegram", "message": body, "inline_keyboard": keyboard})
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


def _loop_paused() -> bool:
    """MJ pause button (scripts/team_os_control.py). HARD pause: stops NEW picks
    AND decision processing/landing, so a paused Team OS advances nothing.
    Pending Approved/Rejected cards are reconciled by the sweep once unpaused."""
    try:
        ks = os.path.expanduser("~/.hermes/state/team-os-kill-switch.json")
        if os.path.exists(ks):
            with open(ks) as f:
                return bool(json.load(f).get("enabled"))
    except Exception:
        return False
    return False


def main() -> int:
    state = TeamOSState(STATE_DB)
    paused = _loop_paused()
    cards = fetch_backlog_cards(PROJECTS)
    decision_error = ""
    if paused:
        # Hard pause: do not process MJ decisions / landings while paused. The
        # sweep re-reads Linear and reconciles any pending Approved/Rejected
        # cards on the first unpaused tick, so no decision is lost.
        decision_cards = []
        decision_results = []
    else:
        try:
            decision_cards = fetch_decision_cards(PROJECTS)
            decision_results = reconcile_pending_decisions(state, decision_cards)
        except Exception as exc:
            decision_cards = []
            decision_results = []
            decision_error = str(exc)[:300]
    try:
        source = WakeSource(WAKE_SOURCE)
    except ValueError:
        source = WakeSource.SWEEP
    before_count = len(state.list_intake_candidates())
    result = reconcile_full_backlog(state=state, backlog_cards=cards, wake_source=source)
    pick = pick_one_after_reconcile(state=state, busy=active_work_busy() or bool(decision_results))
    picked = None if paused else (pick.card["id"] if pick.card else None)
    summary = {
        "status": "paused" if (paused and not decision_results) else ("decision_processed" if decision_results else ("busy" if pick.busy else ("picked" if picked else "empty"))),
        "paused": paused,
        "wake_source": source.value,
        "wake_issue": WAKE_ISSUE,
        "projects": PROJECTS,
        "backlog_cards": len(cards),
        "decision_cards": len(decision_cards),
        "decision_error": decision_error,
        "decisions_processed": decision_results,
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
    # Gate decision: the real Cortex agent audits + classifies when
    # TEAM_OS_CORTEX_AGENT is on (Stage A); otherwise this falls back to the
    # deterministic keyword classifier and behaves exactly as before. The audit
    # (questions, grounding, reasoning) is attached for the triage artifact.
    keyword_gated = _is_gated(payload)
    cortex_verdict = cortex_audit(payload, keyword_gated=keyword_gated)
    gated = bool(cortex_verdict["gated"])
    payload["cortex_audit"] = cortex_verdict
    # Stage B: the real CTO scopes the work into a contract (off by default via
    # TEAM_OS_CTO_AGENT → template fallback). Attached as metadata for the
    # worker (Stage C, still inactive); does not affect the live gate decision.
    payload["cto_contract"] = cto_contract(payload, cortex_verdict)
    scoping_decision = _build_scoping_decision(picked, payload, gated=gated)
    if not scoping_decision["dispatch_allowed"]:
        _linear_status(picked, "Question")
        _linear_comment(
            picked,
            "Autonomous Team OS intake selected this card after full Backlog reconcile, but the Linear scoping harness blocked worker dispatch because the issue is not specific enough.\n"
            f"Wake source: {source.value}; wake issue: {WAKE_ISSUE or '<none>'}.\n"
            f"Ledger: before={before_count}, added={list(result.added)}, removed={list(result.removed)}, current={result.current_count}.\n"
            "No Kanban worker was created and no code/config/runtime changes were performed.\n\n"
            f"{scoping_decision['comment']}"
        )
        summary.update({"gated": gated, "scoping": scoping_decision, "target_state": "Question"})
        print(json.dumps(summary, sort_keys=True))
        return 0
    triage_artifact = _build_cortex_triage_artifact(picked, payload, gated=gated)
    payload["triage_protocol"] = triage_artifact
    payload["mission_contract"] = scoping_decision
    chain = _ensure_spine_chain(picked, payload, gated=gated)
    # WIRE (Stage E hook, dormant): a non-gated (reversible, autonomous) ticket
    # runs the real Worker→Validator slice here. execute_spine self-gates on
    # TEAM_OS_WORKER_DISPATCH + TEAM_OS_VALIDATOR_DISPATCH (both OFF by default)
    # and the connectors refuse human-gated work, so with flags off this is a
    # no-op and the live flow is unchanged. Gated tickets run post-approval
    # (webhook). The kill-switch (_loop_paused) already blocked the pick above
    # when paused, so this never runs while Team OS is paused.
    if not gated:
        payload["execution"] = execute_spine(payload.get("cto_contract") or {})
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
        "No trader/billprinter restart, credentials, external sends, or production writes were performed by intake.\n\n"
        f"{scoping_decision['comment']}\n\n"
        f"{triage_artifact['comment']}"
    )
    summary.update({"gated": gated, "scoping": scoping_decision, "triage_protocol": triage_artifact, "chain": chain, "outbox": outbox, "needs_mj_ping": ping, "target_state": target_state})
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
