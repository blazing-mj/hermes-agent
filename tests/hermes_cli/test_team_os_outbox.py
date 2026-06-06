from __future__ import annotations


def _make_state(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(tmp_path / "team_os.db")
    state.init_schema()
    return state


def test_queue_for_dispatch_creates_queued_row(tmp_path):
    state = _make_state(tmp_path)

    event_id = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-179",
        source="linear",
        payload={"title": "Stage 4"},
    )

    row = state.get_outbox_event(event_id)
    assert row["state"] == "queued"
    assert row["attempt_count"] == 0
    assert row["payload"]["title"] == "Stage 4"


def test_queue_for_dispatch_dedupes_across_poll_cycles(tmp_path):
    state = _make_state(tmp_path)

    first = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-179",
        source="linear",
        payload={"title": "first"},
    )
    second = state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-179",
        source="linear",
        payload={"title": "second"},
    )

    assert first == second
    rows = state.list_outbox_events()
    assert len(rows) == 1
    assert rows[0]["payload"]["title"] == "first"


def test_mark_dispatching_happens_before_success_and_increments_attempt(tmp_path):
    state = _make_state(tmp_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-1", source="linear", payload={}
    )

    state.mark_event_dispatching(event_id)
    dispatching = state.get_outbox_event(event_id)
    assert dispatching["state"] == "dispatching"
    assert dispatching["attempt_count"] == 1
    assert dispatching["dispatching_at"] is not None

    state.mark_event_succeeded(event_id)
    succeeded = state.get_outbox_event(event_id)
    assert succeeded["state"] == "succeeded"
    assert succeeded["completed_at"] is not None
    assert state.list_pending_events() == []


def test_reconcile_in_flight_abandons_without_requeue(tmp_path):
    state = _make_state(tmp_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-1", source="linear", payload={}
    )
    state.mark_event_dispatching(event_id)

    abandoned = state.reconcile_in_flight(reason="restart")

    assert [row["id"] for row in abandoned] == [event_id]
    row = state.get_outbox_event(event_id)
    assert row["state"] == "abandoned"
    assert row["escalation_required"] is True
    assert row["last_error"] == "restart"
    assert state.list_pending_events() == []
    # Idempotency tombstone remains: no silent redrive after abandonment.
    again = state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-1", source="linear", payload={}
    )
    assert again == event_id
    assert state.get_outbox_event(event_id)["state"] == "abandoned"
