from __future__ import annotations


def _state(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(tmp_path / "team_os.db")
    state.init_schema()
    return state


def _card(card_id: str, priority="Medium", age=0):
    return {"id": card_id, "headline": f"{card_id} headline", "priority": priority, "age": age}


def test_doorbell_during_busy_session_does_not_interrupt_current_card(tmp_path):
    from hermes_cli.team_os.intake_reconcile import WakeSource, pick_one_after_reconcile, reconcile_full_backlog

    state = _state(tmp_path)
    reconcile_full_backlog(state=state, backlog_cards=[_card("AGENTS-1")], wake_source=WakeSource.DOORBELL)

    pick = pick_one_after_reconcile(state=state, busy=True)

    assert pick.card is None
    assert pick.busy is True
    assert pick.recheck_requested is True
    assert state.pop_intake_recheck_requested() is True
    # The card stays in the durable ledger for the completion wake.
    assert [c["id"] for c in state.list_intake_candidates()] == ["AGENTS-1"]


def test_full_reconcile_self_heals_four_new_tickets_with_one_lost_webhook(tmp_path):
    from hermes_cli.team_os.intake_reconcile import WakeSource, reconcile_full_backlog

    state = _state(tmp_path)
    # Doorbell saw only three cards.
    first = reconcile_full_backlog(
        state=state,
        backlog_cards=[_card("AGENTS-1"), _card("AGENTS-2"), _card("AGENTS-3")],
        wake_source=WakeSource.DOORBELL,
    )
    assert set(first.added) == {"AGENTS-1", "AGENTS-2", "AGENTS-3"}

    # Later completion/sweep wake scans the full Backlog and recovers the lost webhook.
    second = reconcile_full_backlog(
        state=state,
        backlog_cards=[_card("AGENTS-1"), _card("AGENTS-2"), _card("AGENTS-3"), _card("AGENTS-4")],
        wake_source=WakeSource.COMPLETION,
    )

    assert second.added == ("AGENTS-4",)
    assert second.current_count == 4
    assert {c["id"] for c in state.list_intake_candidates()} == {"AGENTS-1", "AGENTS-2", "AGENTS-3", "AGENTS-4"}


def test_urgent_beats_older_non_urgent_at_next_pick(tmp_path):
    from hermes_cli.team_os.intake_reconcile import WakeSource, pick_one_after_reconcile, reconcile_full_backlog

    state = _state(tmp_path)
    reconcile_full_backlog(
        state=state,
        backlog_cards=[_card("OLD-HIGH", priority="High", age=1000), _card("NEW-URGENT", priority="Urgent", age=1)],
        wake_source=WakeSource.COMPLETION,
    )

    pick = pick_one_after_reconcile(state=state, busy=False)

    assert pick.card is not None
    assert pick.card["id"] == "NEW-URGENT"


def test_oldest_wins_within_same_priority(tmp_path):
    from hermes_cli.team_os.intake_reconcile import WakeSource, pick_one_after_reconcile, reconcile_full_backlog

    state = _state(tmp_path)
    reconcile_full_backlog(
        state=state,
        backlog_cards=[_card("NEW", priority="High", age=1), _card("OLD", priority="High", age=100)],
        wake_source=WakeSource.DOORBELL,
    )

    pick = pick_one_after_reconcile(state=state)
    assert pick.card is not None
    assert pick.card["id"] == "OLD"


def test_sweep_uses_same_reconcile_path_and_removes_missing_backlog_cards(tmp_path):
    from hermes_cli.team_os.intake_reconcile import WakeSource, reconcile_full_backlog

    state = _state(tmp_path)
    reconcile_full_backlog(state=state, backlog_cards=[_card("KEEP"), _card("DONE")], wake_source=WakeSource.DOORBELL)

    result = reconcile_full_backlog(state=state, backlog_cards=[_card("KEEP")], wake_source=WakeSource.SWEEP)

    assert result.wake_source is WakeSource.SWEEP
    assert result.removed == ("DONE",)
    assert [c["id"] for c in state.list_intake_candidates()] == ["KEEP"]


def test_decision_sweep_requeues_approved_pending_mj_review(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("team_os_linear_intake_motor", Path("scripts/team_os_linear_intake_motor.py"))
    motor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(motor)

    state = _state(tmp_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-5",
        source="linear",
        payload={"source_id": "AGENTS-5", "title": "Gated card"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(motor, "_linear_comment", lambda ticket, body: comments.append((ticket, body)))
    monkeypatch.setattr(
        motor,
        "_unblock_approved_kanban_worker",
        lambda ticket, project: {"board": "openclaw-core", "worker": "t_worker", "unblocked": True},
    )

    processed = motor.reconcile_pending_decisions(
        state,
        [{"id": "AGENTS-5", "state": "Approved", "note": "Approved by MJ", "project": "OpenClaw Core"}],
    )

    assert processed == [
        {
            "id": "AGENTS-5",
            "decision": "Approved",
            "new_state": "queued",
            "kanban": {"board": "openclaw-core", "worker": "t_worker", "unblocked": True},
            "integrator": {"status": "did_not_fire", "board": "openclaw-core", "reason": "worker task not found"},
        }
    ]
    row = state.get_outbox_event(event_id)
    assert row["state"] == "queued"
    assert row["payload"]["mj_notes"] == "Approved by MJ"
    assert comments == [
        (
            "AGENTS-5",
            "Decision sweep saw Linear Approved, queued Team OS continuation, unblocked the worker, and ran Integrator. This is the fallback path for a missed webhook delivery. Kanban worker unblock: {'board': 'openclaw-core', 'worker': 't_worker', 'unblocked': True}. Integrator: {'status': 'did_not_fire', 'board': 'openclaw-core', 'reason': 'worker task not found'}.",
        )
    ]


def test_decision_sweep_fails_rejected_pending_mj_review(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("team_os_linear_intake_motor", Path("scripts/team_os_linear_intake_motor.py"))
    motor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(motor)

    state = _state(tmp_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-6",
        source="linear",
        payload={"source_id": "AGENTS-6", "title": "Gated card"},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(motor, "_linear_comment", lambda ticket, body: comments.append((ticket, body)))

    processed = motor.reconcile_pending_decisions(
        state,
        [{"id": "AGENTS-6", "state": "Rejected", "note": "Do not continue"}],
    )

    assert processed == [{"id": "AGENTS-6", "decision": "Rejected", "new_state": "failed"}]
    row = state.get_outbox_event(event_id)
    assert row["state"] == "failed"
    assert "Do not continue" in (row["last_error"] or "")
    assert comments == [("AGENTS-6", "Decision sweep saw Linear Rejected and stopped Team OS continuation. This is the fallback path for a missed webhook delivery.")]


def test_decision_sweep_ignores_cards_without_pending_outbox_row(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("team_os_linear_intake_motor", Path("scripts/team_os_linear_intake_motor.py"))
    motor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(motor)

    state = _state(tmp_path)
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(motor, "_linear_comment", lambda ticket, body: comments.append((ticket, body)))

    processed = motor.reconcile_pending_decisions(state, [{"id": "AGENTS-404", "state": "Approved", "note": "Approved"}])

    assert processed == []
    assert comments == []


def _load_intake_motor():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("team_os_linear_intake_motor", Path("scripts/team_os_linear_intake_motor.py"))
    motor = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(motor)
    return motor


def test_tuned_triage_classifier_allows_reversible_trader_mentions_without_restart():
    motor = _load_intake_motor()

    payload = {
        "title": "Fix trader daemon launchd plists without trader restart",
        "body": "Scope: reversible code/tests/docs only. No daemon restart, no STOP_FLAG clear, no live trading, no money actions.",
        "labels": ["type:rail"],
    }

    assert motor._is_gated(payload) is False


def test_tuned_triage_classifier_still_gates_actual_restart_or_credential_work():
    motor = _load_intake_motor()

    assert motor._is_gated({"title": "Restart trader daemon and clear STOP_FLAG", "body": "", "labels": []}) is True
    assert motor._is_gated({"title": "Rotate production credential", "body": "touch live API key", "labels": []}) is True


def test_tuned_triage_classifier_allows_triage_protocol_rail_despite_access_words():
    motor = _load_intake_motor()

    payload = {
        "title": "Implement Cortex Triage Protocol v1 (audit, dual brief, ask-MJ, access requests)",
        "body": "Reversible code/tests/docs rail work. The protocol may ask for access later but this implementation does not provision credentials or touch production.",
        "labels": ["type:rail"],
    }

    assert motor._is_gated(payload) is False


def test_triage_protocol_requirement_words_do_not_self_mark_ticket_stale():
    motor = _load_intake_motor()

    payload = {
        "title": "Implement Cortex Triage Protocol v1",
        "body": "Acceptance: a stale ticket -> audited+cancelled; a vague ticket -> structured ask.",
        "labels": ["type:rail"],
    }

    assert motor._build_cortex_triage_artifact("AGENTS-237", payload, gated=False)["audit"] == "RELEVANT"


def test_cortex_triage_artifact_contains_audit_dual_brief_ask_and_classification():
    motor = _load_intake_motor()

    payload = {
        "title": "Vague access-needed cleanup",
        "body": "Need access to inspect production customer export before deciding.",
        "url": "https://linear.example/AGENTS-999",
        "labels": ["type:ops"],
        "project": "Hermes System",
    }

    result = motor._build_cortex_triage_artifact("AGENTS-999", payload, gated=True)

    assert result["audit"] == "RELEVANT"
    body = result["comment"]
    assert "Cortex Triage Protocol v1" in body
    assert "AUDIT: RELEVANT" in body
    assert "Human brief" in body
    assert "Agent brief" in body
    assert "type: access" in body
    assert "CLASSIFICATION: Needs-MJ" in body


def test_spine_body_carries_triage_dual_brief_to_cto_worker_validator():
    motor = _load_intake_motor()

    payload = {
        "title": "Reversible rail change",
        "body": "Docs and tests only.",
        "project": "Hermes System",
        "triage_protocol": {"comment": "Cortex Triage Protocol v1\nHuman brief: hello\nAgent brief: file:line proof"},
    }

    body = motor._spine_body("AGENTS-1000", payload, "cto", gated=False)

    assert "Cortex Triage Protocol v1" in body
    assert "Human brief: hello" in body
    assert "Agent brief: file:line proof" in body
