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
