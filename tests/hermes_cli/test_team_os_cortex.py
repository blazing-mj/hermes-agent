from __future__ import annotations


class _Health:
    def __init__(self, healthy: bool, message: str = "health"):
        self.healthy = healthy
        self.message = message


def _make_state(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(tmp_path / "team_os.db")
    state.init_schema()
    return state


def _observation(source_id="AGENTS-179"):
    from hermes_cli.team_os.schema import Observation

    return Observation(source="linear", source_id=source_id, title="Stage 4")


def test_cortex_config_defaults_no_live_dispatch():
    from hermes_cli.team_os.cortex import CortexConfig

    cfg = CortexConfig()
    assert cfg.active is False
    assert cfg.dry_run is True


def test_cortex_reconciles_before_polling(tmp_path):
    from hermes_cli.team_os.cortex import CortexConfig, run_cortex

    state = _make_state(tmp_path)
    event_id = state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-old", source="linear", payload={}
    )
    state.mark_event_dispatching(event_id)

    result = run_cortex(state, CortexConfig(), observations=[_observation("AGENTS-new")])

    assert result.reconcile_count == 1
    assert result.reconciled[0]["state"] == "abandoned"
    assert [event.source_id for event in result.queued] == ["AGENTS-new"]
    assert result.dispatched == 0


def test_cortex_dry_run_default_never_calls_dispatch(tmp_path):
    from hermes_cli.team_os.cortex import CortexConfig, run_cortex

    state = _make_state(tmp_path)
    state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-179", source="linear", payload={}
    )

    called = {"count": 0}

    def dispatch(_event):
        called["count"] += 1
        raise AssertionError("dry-run must not dispatch")

    result = run_cortex(
        state,
        CortexConfig(active=False, dry_run=True),
        gateway_health_probe=lambda: _Health(True),
        dispatch=dispatch,
    )

    assert result.dispatched == 0
    assert called["count"] == 0
    assert "disabled" in (result.paused_reason or "")


def test_cortex_active_pauses_when_gateway_unhealthy(tmp_path):
    from hermes_cli.team_os.cortex import CortexConfig, run_cortex

    state = _make_state(tmp_path)
    state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-179", source="linear", payload={}
    )
    called = {"count": 0}

    def dispatch(_event):
        called["count"] += 1

    result = run_cortex(
        state,
        CortexConfig(active=True, dry_run=False),
        gateway_health_probe=lambda: _Health(False, "gateway down"),
        dispatch=dispatch,
    )

    assert result.dispatched == 0
    assert called["count"] == 0
    assert result.paused_reason == "gateway down"
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-179")
    assert row is not None
    assert row["state"] == "queued"


def test_cortex_active_marks_dispatching_before_success(tmp_path):
    from hermes_cli.team_os.cortex import CortexConfig, run_cortex

    state = _make_state(tmp_path)
    state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-179", source="linear", payload={}
    )
    seen_states = []

    def dispatch(event):
        seen_states.append(state.get_outbox_event(int(event["id"]))["state"])

    result = run_cortex(
        state,
        CortexConfig(active=True, dry_run=False),
        gateway_health_probe=lambda: _Health(True),
        dispatch=dispatch,
    )

    assert seen_states == ["dispatching"]
    assert result.dispatched == 1
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-179")
    assert row is not None
    assert row["state"] == "succeeded"


def test_cortex_active_marks_needs_mj_dispatch_as_mj_review(tmp_path):
    from hermes_cli.team_os.cortex import CortexConfig, run_cortex

    state = _make_state(tmp_path)
    state.queue_for_dispatch(
        event_type="linear_observation", source_id="AGENTS-193", source="linear", payload={}
    )

    def dispatch(_event):
        return {"status": "needs_mj", "reason": "human gate required"}

    result = run_cortex(
        state,
        CortexConfig(active=True, dry_run=False),
        gateway_health_probe=lambda: _Health(True),
        dispatch=dispatch,
    )

    assert result.dispatched == 1
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-193")
    assert row is not None
    assert row["state"] == "mj_review"
    assert row["escalation_required"] is True
    assert row["last_error"] == "human gate required"


def test_team_os_cortex_cli_dry_run_outputs_disabled_state(tmp_path, capsys):
    import argparse
    import json

    from hermes_cli.team_os.cli import cmd_team_os, register_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    team_os = sub.add_parser("team-os")
    register_cli(team_os)

    args = parser.parse_args([
        "team-os",
        "cortex",
        "--state-db",
        str(tmp_path / "state.db"),
    ])
    rc = cmd_team_os(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["active"] is False
    assert payload["dispatched"] == 0


def test_cortex_plist_is_separate_disabled_daemon():
    from hermes_cli.team_os.cortex import CORTEX_LAUNCHD_LABEL, CORTEX_PLIST_TEMPLATE

    assert CORTEX_LAUNCHD_LABEL == "ai.hermes.team-os-cortex"
    assert "ai.hermes.gateway" not in CORTEX_PLIST_TEMPLATE
    assert "<false/>" in CORTEX_PLIST_TEMPLATE  # not RunAtLoad/KeepAlive live by template
    assert "--live-dispatch" not in CORTEX_PLIST_TEMPLATE


def test_gateway_health_required_blocks_production_gate(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import LoopTask
    from hermes_cli.team_os.production_gate import check_production_gate

    task = LoopTask(
        task_id="T1",
        title="ready",
        approval_status="approved",
        quota_confidence="high",
        task_confidence="high",
    )
    result = check_production_gate(
        task,
        kill_switch=KillSwitch(tmp_path / "ks.json"),
        require_gateway_health=True,
    )
    assert result.passed is False
    assert any("gateway health probe" in violation for violation in result.violations)

    result2 = check_production_gate(
        task,
        kill_switch=KillSwitch(tmp_path / "ks.json"),
        require_gateway_health=True,
        gateway_health_probe=lambda: _Health(False, "gateway down"),
    )
    assert result2.passed is False
    assert any("gateway down" in violation for violation in result2.violations)
