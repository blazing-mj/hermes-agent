from __future__ import annotations


def _make_state(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(tmp_path / "team_os.db")
    state.init_schema()
    return state


def _observation(source_id="AGENTS-179", title="Stage 4"):
    from hermes_cli.team_os.schema import Observation

    return Observation(
        source="linear",
        source_id=source_id,
        title=title,
        body="body",
        status="In Progress",
        project="Hermes System",
        labels=["system:hermes", "type:rail"],
        url=f"https://linear.app/blazeragency/issue/{source_id}",
    )


def test_route_linear_observation_queues_outbox_event(tmp_path):
    from hermes_cli.team_os.event_router import route_linear_observation

    state = _make_state(tmp_path)
    event = route_linear_observation(_observation(), state)

    assert event is not None
    assert event.event_type == "linear_observation"
    assert event.source_id == "AGENTS-179"
    assert event.status == "queued"
    row = state.get_outbox_event(event.event_id)
    assert row["payload"]["project"] == "Hermes System"


def test_same_linear_observation_across_two_polls_creates_one_row(tmp_path):
    from hermes_cli.team_os.event_router import route_linear_observation

    state = _make_state(tmp_path)
    first = route_linear_observation(_observation("AGENTS-179", "first"), state)
    second = route_linear_observation(_observation("AGENTS-179", "second"), state)

    assert first is not None and second is not None
    assert first.event_id == second.event_id
    rows = state.list_outbox_events()
    assert len(rows) == 1
    assert rows[0]["payload"]["title"] == "first"


def test_high_failure_cost_observation_is_held_for_mj_review(tmp_path):
    from hermes_cli.team_os.event_router import route_linear_observation

    state = _make_state(tmp_path)
    event = route_linear_observation(
        _observation("AGENTS-176", "Rotate credential token"), state
    )

    assert event is None
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-176")
    assert row is not None
    assert row["state"] == "mj_review"
    assert row["escalation_required"] is True
    assert row["payload"]["requires_mj_review"] is True
    assert row["payload"]["failure_cost_tier"] == "critical"


def test_terminal_outbox_row_is_skipped_not_requeued(tmp_path):
    from hermes_cli.team_os.event_router import route_linear_observation

    state = _make_state(tmp_path)
    event = route_linear_observation(_observation("AGENTS-180"), state)
    assert event is not None
    state.mark_event_dispatching(event.event_id)

    skipped = route_linear_observation(_observation("AGENTS-180"), state)

    assert skipped is None
    assert len(state.list_outbox_events()) == 1
