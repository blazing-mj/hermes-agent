from __future__ import annotations

import json

from hermes_cli.team_os.db import TeamOSState


def _issue_payload(state_name: str, *, issue_id: str = "AGENTS-205", previous: str = "Needs-MJ", comment_body: str | None = None) -> dict:
    payload = {
        "action": "update",
        "type": "Issue",
        "webhookTimestamp": 1780930000000,
        "updatedFrom": {"state": {"name": previous}},
        "data": {
            "id": "issue-uuid",
            "identifier": issue_id,
            "title": "Approval UX blocker",
            "url": f"https://linear.app/blazeragency/issue/{issue_id.lower()}",
            "state": {"name": state_name},
        },
    }
    if comment_body is not None:
        payload["data"]["comments"] = {"nodes": [{"body": comment_body}]}
    return payload


def _comment_payload(*, issue_id: str = "AGENTS-205", state_name: str = "Needs-MJ", body: str = "What changes after approval?", user_name: str = "MJ") -> dict:
    return {
        "action": "create",
        "type": "Comment",
        "webhookTimestamp": 1780930000001,
        "data": {
            "id": "comment-uuid",
            "body": body,
            "issue": {
                "id": "issue-uuid",
                "identifier": issue_id,
                "title": "Approval UX blocker",
                "url": f"https://linear.app/blazeragency/issue/{issue_id.lower()}",
                "state": {"name": state_name},
            },
            "user": {"name": user_name},
        },
    }


def test_approved_status_requeues_mj_review_event_and_comments_with_notes(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker", "project": "Hermes System"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []
    wakes: list[dict] = []
    integrator_calls: list[dict] = []

    import hermes_cli.team_os.linear_webhook as linear_webhook
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(
        linear_webhook,
        "unblock_approved_kanban_worker",
        lambda ticket, project: {"board": "hermes-system", "worker": "t_worker", "unblocked": True},
    )
    try:
        result = handle_linear_webhook(
            _issue_payload("Approved", comment_body="Approved, but keep Phase 6 stopped."),
            state=state,
            add_comment=lambda issue_id, body: comments.append((issue_id, body)),
            run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True, "pid": 4321},
            run_integrator_auto_land=lambda **kwargs: integrator_calls.append(kwargs) or {"status": "auto_landed", "fyi_sent": True},
        )
    finally:
        monkeypatch.undo()

    assert result["decision"] == "approved"
    row = state.get_outbox_event(event_id)
    assert row["state"] == "succeeded"
    assert "Approved, but keep Phase 6 stopped." in row["payload"]["mj_notes"]
    assert row["payload"]["integrator_auto_land"]["status"] == "auto_landed"
    assert comments == [("AGENTS-205", "Approved received — Team OS queued continuation with MJ notes attached, cleared the Kanban worker block ({'board': 'hermes-system', 'worker': 't_worker', 'unblocked': True}), woke the intake/dispatch motor, and ran Integrator ({'status': 'auto_landed', 'fyi_sent': True}).")]
    assert wakes == [{"issue_id": "AGENTS-205", "wake_source": "completion"}]
    assert integrator_calls == [{"ticket": "AGENTS-205", "project": "Hermes System", "notes": "Approved, but keep Phase 6 stopped."}]
    assert result["kanban"] == {"board": "hermes-system", "worker": "t_worker", "unblocked": True}
    assert result["integrator"] == {"status": "auto_landed", "fyi_sent": True}
    assert result["started"] is True


def test_rejected_status_with_comment_fails_closed_for_revision(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []

    result = handle_linear_webhook(
        _issue_payload("Rejected", comment_body="Wrong UX, revise."),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
    )

    assert result["decision"] == "rejected"
    row = state.get_outbox_event(event_id)
    assert row["state"] == "failed"
    assert "Wrong UX, revise." in (row["last_error"] or "")
    assert comments == [("AGENTS-205", "Rejected received — Team OS will not continue this card. MJ notes were captured for revise/archive handling.")]


def test_question_lane_or_comment_on_needs_mj_gets_linear_reply_without_requeue(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []

    result = handle_linear_webhook(
        _comment_payload(body="QUESTION: What exactly am I approving?"),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
    )

    assert result["decision"] == "question"
    assert state.get_outbox_event(event_id)["state"] == "mj_review"
    assert len(comments) == 1
    assert comments[0][0] == "AGENTS-205"
    assert "You are approving whether Team OS may continue past this human gate" in comments[0][1]
    assert "Problem → fix → new behavior → what approval allows" in comments[0][1]


def test_approved_comment_on_needs_mj_requeues_event(tmp_path, monkeypatch):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker", "project": "Hermes System"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []
    wakes: list[dict] = []
    import hermes_cli.team_os.linear_webhook as linear_webhook
    monkeypatch.setattr(
        linear_webhook,
        "unblock_approved_kanban_worker",
        lambda ticket, project: {"board": "hermes-system", "worker": "t_worker", "unblocked": True},
    )

    result = handle_linear_webhook(
        _comment_payload(body="APPROVED AGENTS-205 — continue but keep Phase 6 blocked."),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
        run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True},
        run_integrator_auto_land=lambda **kwargs: {"status": "auto_landed", "fyi_sent": True},
    )

    assert result["decision"] == "approved"
    row = state.get_outbox_event(event_id)
    assert row["state"] == "succeeded"
    assert "keep Phase 6 blocked" in row["payload"]["mj_notes"]
    assert comments == [("AGENTS-205", "Approved received — Team OS queued continuation with MJ notes attached, cleared the Kanban worker block ({'board': 'hermes-system', 'worker': 't_worker', 'unblocked': True}), woke the intake/dispatch motor, and ran Integrator ({'status': 'auto_landed', 'fyi_sent': True}).")]
    assert wakes == [{"issue_id": "AGENTS-205", "wake_source": "completion"}]
    assert result["kanban"] == {"board": "hermes-system", "worker": "t_worker", "unblocked": True}


def test_rejected_comment_on_needs_mj_fails_closed(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []

    result = handle_linear_webhook(
        _comment_payload(body="NOT APPROVED: revise UX first."),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
    )

    assert result["decision"] == "rejected"
    row = state.get_outbox_event(event_id)
    assert row["state"] == "failed"
    assert "revise UX first" in (row["last_error"] or "")
    assert comments == [("AGENTS-205", "Rejected received — Team OS will not continue this card. MJ notes were captured for revise/archive handling.")]


def test_low_cost_non_needs_mj_webhook_is_ignored(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    comments: list[tuple[str, str]] = []

    result = handle_linear_webhook(
        _issue_payload("Approved", issue_id="AGENTS-999", previous="In Progress"),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
    )

    assert result["decision"] == "ignored"
    assert comments == []


def test_late_approved_retry_after_sweep_is_duplicate_not_requeued(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-205",
        source="linear",
        payload={"source_id": "AGENTS-205", "title": "Approval UX blocker"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    # Simulate the sweep fallback already processing MJ's approval before a late webhook retry arrives.
    row = state.get_outbox_event(event_id)
    from hermes_cli.team_os.linear_webhook import apply_mj_decision
    apply_mj_decision(state, row, decision="Approved")
    comments: list[tuple[str, str]] = []
    wakes: list[dict] = []

    result = handle_linear_webhook(
        _issue_payload("Approved", issue_id="AGENTS-205", previous="Needs-MJ"),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
        run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True},
    )

    assert result == {"decision": "duplicate", "issue": "AGENTS-205", "outbox_state": "queued"}
    assert state.get_outbox_event(event_id)["state"] == "queued"
    assert comments == []
    assert wakes == []


def test_issue_created_in_backlog_rings_cortex_intake_doorbell(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    comments: list[tuple[str, str]] = []
    wakes: list[dict] = []

    payload = _issue_payload("Backlog", issue_id="AGENTS-225", previous="")
    payload["action"] = "create"

    result = handle_linear_webhook(
        payload,
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
        run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True, "pid": 1234},
    )

    assert result == {"decision": "doorbell", "issue": "AGENTS-225", "started": True, "pid": 1234}
    assert wakes == [{"issue_id": "AGENTS-225", "wake_source": "doorbell"}]
    assert comments == []


def test_issue_update_into_backlog_rings_cortex_intake_doorbell(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    wakes: list[dict] = []

    result = handle_linear_webhook(
        _issue_payload("Backlog", issue_id="AGENTS-226", previous="Triage"),
        state=state,
        add_comment=lambda _issue_id, _body: None,
        run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True},
    )

    assert result["decision"] == "doorbell"
    assert wakes == [{"issue_id": "AGENTS-226", "wake_source": "doorbell"}]


def test_issue_update_outside_backlog_does_not_ring_intake_doorbell(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    wakes: list[dict] = []

    result = handle_linear_webhook(
        _issue_payload("In Progress", issue_id="AGENTS-226", previous="Backlog"),
        state=state,
        add_comment=lambda _issue_id, _body: None,
        run_intake_wake=lambda **kwargs: wakes.append(kwargs) or {"started": True},
    )

    assert result["decision"] == "ignored"
    assert wakes == []


def test_team_os_linear_webhook_ignores_its_own_reply_comments(tmp_path):
    from hermes_cli.team_os.linear_webhook import handle_linear_webhook

    state = TeamOSState(tmp_path / "team-os.db")
    comments: list[tuple[str, str]] = []

    result = handle_linear_webhook(
        _comment_payload(body="Answer for AGENTS-205 — Approval UX blocker:\n\nYou are approving whether Team OS may continue past this human gate."),
        state=state,
        add_comment=lambda issue_id, body: comments.append((issue_id, body)),
    )

    assert result["decision"] == "ignored"
    assert result["reason"] == "self comment"
    assert comments == []


def test_handle_team_os_linear_webhook_default_db_matches_intake_motor_env(tmp_path, monkeypatch):
    import hermes_cli.team_os.linear_webhook as linear_webhook

    db_path = tmp_path / "team-os-cortex.db"
    state = TeamOSState(db_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-5",
        source="linear",
        payload={"source_id": "AGENTS-5", "title": "Gated card", "project": "OpenClaw Core"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []
    monkeypatch.setenv("TEAM_OS_STATE_DB", str(db_path))
    monkeypatch.setenv("TEAM_OS_CORTEX_INTAKE_SCRIPT", str(tmp_path / "missing-intake.sh"))
    monkeypatch.setattr(linear_webhook, "add_linear_comment", lambda issue_id, body: comments.append((issue_id, body)))
    monkeypatch.setattr(
        linear_webhook,
        "unblock_approved_kanban_worker",
        lambda ticket, project: {"board": "openclaw-core", "worker": "t_worker", "unblocked": True},
    )
    monkeypatch.setattr(linear_webhook, "run_integrator_auto_land", lambda **kwargs: {"status": "auto_landed", "fyi_sent": True})

    result = linear_webhook.handle_team_os_linear_webhook(_issue_payload("Approved", issue_id="AGENTS-5"))

    assert result["decision"] == "approved"
    assert result["reason"] == "intake script missing"
    assert TeamOSState(db_path).get_outbox_event(event_id)["state"] == "succeeded"
    assert comments == [("AGENTS-5", "Approved received — Team OS queued continuation with MJ notes attached, cleared the Kanban worker block ({'board': 'openclaw-core', 'worker': 't_worker', 'unblocked': True}), woke the intake/dispatch motor, and ran Integrator ({'status': 'auto_landed', 'fyi_sent': True}).")]


def test_linear_webhook_adapter_handles_linear_signature_and_invokes_team_os_handler(monkeypatch):
    import hashlib
    import hmac
    from aiohttp import web
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    secret = "linear-secret"
    payload = _comment_payload()
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    called: list[dict] = []

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "routes": {
                    "linear-team-os": {
                        "secret": secret,
                        "team_os_linear": True,
                        "state_db": ":memory:",
                    }
                }
            },
        )
    )

    async def fake_handler(payload_arg, **_kwargs):  # noqa: ANN001
        called.append(payload_arg)
        return {"decision": "question", "issue": "AGENTS-205"}

    monkeypatch.setattr("gateway.platforms.webhook.handle_team_os_linear_webhook", fake_handler, raising=False)

    class Req:
        method = "POST"
        content_length = len(body)
        headers = {"Linear-Signature": signature}
        match_info = {"route_name": "linear-team-os"}

        async def read(self):
            return body

    response = __import__("asyncio").run(adapter._handle_webhook(Req()))

    assert response.status == 200
    assert json.loads(response.text)["status"] == "handled"
    assert called == [payload]


def test_linear_delivery_header_dedupes_retries(monkeypatch):
    import asyncio
    import hashlib
    import hmac
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    secret = "linear-secret"
    payload = _comment_payload()
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    calls: list[dict] = []

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"routes": {"linear-team-os": {"secret": secret, "team_os_linear": True}}},
        )
    )

    async def fake_handler(payload_arg, **_kwargs):  # noqa: ANN001
        calls.append(payload_arg)
        return {"decision": "question", "issue": "AGENTS-205"}

    monkeypatch.setattr("gateway.platforms.webhook.handle_team_os_linear_webhook", fake_handler, raising=False)

    class Req:
        method = "POST"
        content_length = len(body)
        headers = {"Linear-Signature": signature, "Linear-Delivery": "linear-delivery-1"}
        match_info = {"route_name": "linear-team-os"}

        async def read(self):
            return body

    first = asyncio.run(adapter._handle_webhook(Req()))
    second = asyncio.run(adapter._handle_webhook(Req()))

    assert first.status == 200
    assert json.loads(second.text)["status"] == "duplicate"
    assert calls == [payload]
