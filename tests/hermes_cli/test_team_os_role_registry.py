from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.team_os.contracts import render_template
from hermes_cli.team_os.role_registry import assignment_violation, validator_contract_route


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_validator_assignment_to_gateway_profile_fails_closed_on_create(kanban_home):
    with kb.connect() as conn, pytest.raises(ValueError, match="validator task type must route"):
        kb.create_task(conn, title="AGENTS-206 Validator: cold review", assignee="ruta")


def test_validator_assignment_to_gateway_profile_fails_closed_on_assign(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="AGENTS-206 Validator: cold review")
        with pytest.raises(ValueError, match="validator task type must route"):
            kb.assign_task(conn, tid, "default")


def test_dispatcher_blocks_existing_out_of_registry_validator_card(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="AGENTS-206 implementation", assignee="a")
        conn.execute(
            "UPDATE tasks SET title=?, assignee=?, status='ready' WHERE id=?",
            ("AGENTS-206 Validator: cold review", "ruta", tid),
        )
        conn.commit()

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 12345)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert tid in result.auto_blocked
        assert "role registry rejected assignment" in (task.last_failure_error or "")
        assert any(e.kind == "assignment_rejected" for e in kb.list_events(conn, tid))


def test_validator_rail_assignee_is_not_a_gateway_spawn_profile(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="AGENTS-206 Validator: cold review", assignee="claude-max-code")
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 12345)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert tid in result.skipped_nonspawnable
        assert tid not in [spawned[0] for spawned in result.spawned]


def test_cto_validator_contract_template_pins_cold_rail_route():
    template = render_template("validator")
    assert template["validator_route"] == validator_contract_route()
    assert template["validator_route"]["validator_route"] == "run_adversarial_validator"
    assert template["validator_route"]["validator_runner"] == "claude-max-code"
    assert template["validator_route"]["gateway_profiles_allowed"] is False


def test_assignment_violation_names_non_dispatchable_profiles():
    assert "not dispatchable" in (assignment_violation(title="ops task", body="", assignee="billprinter") or "")


def test_control_plane_spine_markers_are_exempt_from_rail_routing():
    """Regression (found by the P0 landing tests): the live motor creates its
    validator spine MARKER as '<ticket> Validator independent proof' assigned to
    'team-os' — the control-plane label the dispatcher skips (dispatcher-jam
    fix). The registry rejected it, which would crash _ensure_spine_chain on
    the next real pick. Markers are never dispatched, so rail rules don't apply."""
    assert assignment_violation(
        title="AGENTS-999 Validator independent proof",
        body="spine marker", assignee="team-os",
    ) is None
    # the actual protection is intact: a REAL rail-routable profile still can't
    # take validator work
    assert assignment_violation(
        title="AGENTS-999 Validator independent proof",
        body="run it", assignee="default",
    ) is not None
