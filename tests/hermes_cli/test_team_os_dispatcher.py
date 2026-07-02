from __future__ import annotations

import json
from pathlib import Path


def _event(
    source_id: str = "AGENTS-172",
    *,
    tier: str = "low",
    title: str = "Planner polish",
    requires_mj_review: bool = False,
    gate: str | None = None,
    classifier_uncertain: bool = False,
) -> dict:
    return {
        "id": 7,
        "event_type": "linear_observation",
        "source_id": source_id,
        "source": "linear",
        "payload": {
            "source": "linear",
            "source_id": source_id,
            "title": title,
            "body": "Polish Planner acceptance criteria; safe docs-only internal change.",
            "status": "Ready",
            "project": "Hermes System",
            "labels": ["system:hermes", "type:docs", "failure-cost:low"],
            "url": f"https://linear.app/blazeragency/issue/{source_id.lower()}",
            "failure_cost_tier": tier,
            "requires_mj_review": requires_mj_review,
            "failure_cost_reason": f"test maps to {tier}",
            "gate": gate,
            "classifier_uncertain": classifier_uncertain,
            "validation_contract": {
                "source_ticket": source_id,
                "intended_behavior": "Polish Planner acceptance criteria into crisp testable conditions",
                "non_goals": ["Do not touch production", "Do not change customer infrastructure"],
                "assertions": ["Worker produces a focused diff", "Required proof passes"],
                "commands": ["python -m py_compile pkg/planner.py"],
                "required_commands": ["python -m py_compile pkg/planner.py"],
                "behavior_check_required": True,
                "risk": "low",
                "human_gate_required": True,
                "bounce_conditions": ["Worker touches blocked surfaces", "Validator returns BOUNCE"],
                "files_to_touch": ["pkg/planner.py"],
            },
        },
        "state": "queued",
    }


def test_dispatcher_runs_low_cost_outbox_event_through_worker_validator_without_human_ping(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    calls: dict[str, object] = {"telegram": [], "assignments": []}

    def worker(*, contract, repo_root, worktree_root, lease_path, branch, timeout_seconds):  # noqa: ANN001
        assert repo_root == tmp_path / "repo"
        assert worktree_root == tmp_path / "workers"
        assert branch.startswith("team-os-agents-172-")
        return {
            "worker_status": "completed",
            "source_ticket": contract["source_ticket"],
            "worktree_path": str(worktree_root / branch),
            "changed_files": ["pkg/planner.py"],
            "proof_results": [{"command": "python -m py_compile pkg/planner.py", "exit_code": 0}],
            "worker_output": "polished planner acceptance criteria",
            "human_gate_required": True,
            "loop_feed_allowed": False,
            "auto_dispatch_allowed": False,
            "auto_done_allowed": False,
        }

    def validator(*, contract_path, handoff_path, state_path):  # noqa: ANN001
        contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
        handoff = json.loads(Path(handoff_path).read_text(encoding="utf-8"))
        assert contract["source_ticket"] == "AGENTS-172"
        assert handoff["worker_status"] == "completed"
        return {
            "verdict": "PASS",
            "source_ticket": "AGENTS-172",
            "review_text": "VERDICT: PASS\nstep_summary: intent=ok scope=ok acceptance=ok implementation=ok proof=ok",
            "auto_done_allowed": False,
            "human_gate_required": True,
        }

    def telegram_push(message: str) -> None:
        calls["telegram"].append(message)  # type: ignore[index]

    result = dispatch_outbox_event(
        _event(),
        DispatcherConfig(
            repo_root=tmp_path / "repo",
            worktree_root=tmp_path / "workers",
            artifact_root=tmp_path / "artifacts",
            lease_root=tmp_path / "leases",
            telegram_push_enabled=True,
        ),
        worker=worker,
        validator=validator,
        telegram_push=telegram_push,
    )

    assert result["status"] == "validated"
    assert result["source_ticket"] == "AGENTS-172"
    assert result["failure_cost_tier"] == "low"
    assert result["worker"]["changed_files"] == ["pkg/planner.py"]
    assert result["validator"]["verdict"] == "PASS"
    assert result["telegram_push"]["sent"] is False
    assert calls["telegram"] == []
    assert calls["assignments"] == []
    assert Path(result["contract_path"]).exists()
    assert Path(result["handoff_path"]).exists()


def test_dispatcher_human_gate_assigns_mj_and_sends_exactly_one_proof_ping_without_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    calls: dict[str, list[str]] = {"telegram": [], "assignments": []}

    def worker(**_kwargs):  # noqa: ANN003
        raise AssertionError("human-gate card must not dispatch to worker")

    event = _event(
        source_id="AGENTS-999",
        tier="critical",
        title="Production credential change",
        requires_mj_review=True,
        gate="human",
        classifier_uncertain=True,
    )
    result = dispatch_outbox_event(
        event,
        DispatcherConfig(
            repo_root=tmp_path / "repo",
            worktree_root=tmp_path / "workers",
            artifact_root=tmp_path / "artifacts",
            lease_root=tmp_path / "leases",
            telegram_push_enabled=True,
        ),
        worker=worker,
        telegram_push=lambda message: calls["telegram"].append(message),
        assign_mj=lambda ticket: calls["assignments"].append(ticket),
    )

    assert result["status"] == "needs_mj"
    assert result["reason"] == "human gate required"
    assert result["linear_assignment"] == {"assignee": "MJ", "set": True}
    assert result["telegram_push"] == {"enabled": True, "sent": True}
    assert calls["assignments"] == ["AGENTS-999"]
    assert len(calls["telegram"]) == 1
    assert "AGENTS-999" in calls["telegram"][0]
    assert "https://linear.app/blazeragency/issue/agents-999" in calls["telegram"][0]


def test_dispatcher_human_gate_does_not_open_telegram_without_assignment_callback(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    calls: list[str] = []
    result = dispatch_outbox_event(
        _event(tier="high", requires_mj_review=True),
        DispatcherConfig(
            repo_root=tmp_path / "repo",
            worktree_root=tmp_path / "workers",
            artifact_root=tmp_path / "artifacts",
            lease_root=tmp_path / "leases",
            telegram_push_enabled=True,
        ),
        worker=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("worker must not run")),
        telegram_push=lambda message: calls.append(message),
    )

    assert result["status"] == "needs_mj"
    assert result["linear_assignment"] == {"assignee": "MJ", "set": False, "reason": "assignment callback not configured"}
    assert result["telegram_push"]["sent"] is False
    assert calls == []


def test_dispatcher_blocks_medium_or_client_prod_money_surfaces_before_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    def worker(**_kwargs):  # noqa: ANN003
        raise AssertionError("worker must not run")

    medium = dispatch_outbox_event(
        _event(tier="medium"),
        DispatcherConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "workers", artifact_root=tmp_path / "artifacts", lease_root=tmp_path / "leases"),
        worker=worker,
    )
    assert medium["status"] == "needs_mj"
    assert medium["linear_assignment"]["assignee"] == "MJ"

    prod = dispatch_outbox_event(
        _event(title="Client production money change", tier="low"),
        DispatcherConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "workers", artifact_root=tmp_path / "artifacts", lease_root=tmp_path / "leases"),
        worker=worker,
    )
    assert prod["status"] == "blocked_surface"
    assert "client" in prod["reason"].lower() or "money" in prod["reason"].lower()


def test_dispatcher_only_auto_dones_low_cost_after_pass_when_callback_supplied(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    done: list[str] = []

    def worker(**kwargs):  # noqa: ANN003
        contract = kwargs["contract"]
        return {
            "worker_status": "completed",
            "source_ticket": contract["source_ticket"],
            "worktree_path": str(tmp_path / "workers" / "branch"),
            "changed_files": ["pkg/planner.py"],
            "proof_results": [{"command": "python -m py_compile pkg/planner.py", "exit_code": 0}],
            "worker_output": "ok",
            "human_gate_required": True,
            "loop_feed_allowed": False,
            "auto_dispatch_allowed": False,
            "auto_done_allowed": False,
        }

    def validator(**_kwargs):  # noqa: ANN003
        return {"verdict": "PASS", "review_text": "VERDICT: PASS", "source_ticket": "AGENTS-172"}

    result = dispatch_outbox_event(
        _event(),
        DispatcherConfig(
            repo_root=tmp_path / "repo",
            worktree_root=tmp_path / "workers",
            artifact_root=tmp_path / "artifacts",
            lease_root=tmp_path / "leases",
            auto_done_low_cost=True,
        ),
        worker=worker,
        validator=validator,
        auto_done=lambda ticket: done.append(ticket),
    )

    assert result["status"] == "validated"
    assert result["auto_done"]["attempted"] is True
    assert result["auto_done"]["done"] is True
    assert done == ["AGENTS-172"]


def test_dispatcher_triggers_integrator_after_validator_pass_when_enabled(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event
    from hermes_cli.team_os.integrator import IntegratorResult

    seen: dict[str, object] = {}

    def worker(**kwargs):  # noqa: ANN003
        contract = kwargs["contract"]
        branch = kwargs["branch"]
        return {
            "worker_status": "completed",
            "source_ticket": contract["source_ticket"],
            "worktree_path": str(kwargs["worktree_root"] / branch),
            "changed_files": ["pkg/planner.py"],
            "proof_results": [{"command": "python -m py_compile pkg/planner.py", "exit_code": 0}],
            "worker_output": "ok",
            "human_gate_required": True,
            "loop_feed_allowed": False,
            "auto_dispatch_allowed": False,
            "auto_done_allowed": False,
        }

    def validator(**_kwargs):  # noqa: ANN003
        return {"verdict": "PASS", "review_text": "VERDICT: PASS", "source_ticket": "AGENTS-172"}

    def integrator(input_data):  # noqa: ANN001
        seen["source_ticket"] = input_data.source_ticket
        seen["handoff_exists"] = input_data.handoff_path.exists()
        seen["validator_exists"] = input_data.validator_report_path.exists()
        seen["deploy_command"] = input_data.deploy_command
        return IntegratorResult(
            status="auto_landed",
            reversibility="reversible",
            rollback_commands=("git revert abc123 --no-edit",),
            fyi_sent=True,
            commands=(("git", "merge", "--ff-only", "feature"), ("hermes", "gateway", "restart")),
        )

    result = dispatch_outbox_event(
        _event(),
        DispatcherConfig(
            repo_root=tmp_path / "repo",
            worktree_root=tmp_path / "workers",
            artifact_root=tmp_path / "artifacts",
            lease_root=tmp_path / "leases",
            integrator_auto_land=True,
            integrator_deploy_command=("hermes", "gateway", "restart"),
            integrator_fyi_counter_path=tmp_path / "fyi.json",
        ),
        worker=worker,
        validator=validator,
        integrator=integrator,
    )

    assert result["status"] == "auto_landed"
    assert result["integrator"]["status"] == "auto_landed"
    assert result["integrator"]["fyi_sent"] is True
    assert seen == {
        "source_ticket": "AGENTS-172",
        "handoff_exists": True,
        "validator_exists": True,
        "deploy_command": (),
    }


def _parse_cortex_args(tmp_path, state_db, *extra):
    import argparse

    from hermes_cli.team_os.cli import register_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    team_os = sub.add_parser("team-os")
    register_cli(team_os)
    return parser.parse_args([
        "team-os",
        "cortex",
        "--state-db",
        str(state_db),
        "--active",
        "--live-dispatch",
        "--repo-root",
        str(tmp_path / "repo"),
        "--worktree-root",
        str(tmp_path / "workers"),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--stub-dispatch-success",
        *extra,
    ])


def _queue_dispatch_fixture(state_db, payload):
    from hermes_cli.team_os.db import TeamOSState

    state = TeamOSState(state_db)
    state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-172",
        source="linear",
        payload=payload,
    )
    return state


def test_cortex_cli_live_dispatch_still_pauses_without_explicit_gateway_health_ok(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    state_db = tmp_path / "state.db"
    state = _queue_dispatch_fixture(state_db, _event()["payload"])
    args = _parse_cortex_args(tmp_path, state_db)

    rc = cmd_team_os(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dispatched"] == 0
    assert payload["paused_reason"] == "gateway/runtime unhealthy"
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-172")
    assert row is not None
    assert row["state"] == "queued"


def test_cortex_cli_wires_dispatcher_when_live_dispatch_and_gateway_health_enabled(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os

    state_db = tmp_path / "state.db"
    state = _queue_dispatch_fixture(state_db, _event()["payload"])
    args = _parse_cortex_args(tmp_path, state_db, "--gateway-health-ok")

    rc = cmd_team_os(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dispatched"] == 1
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-172")
    assert row is not None
    assert row["state"] == "succeeded"


def test_cortex_cli_human_gate_uses_assignment_before_one_telegram_ping(tmp_path, capsys, monkeypatch):
    import subprocess

    from hermes_cli.team_os.cli import cmd_team_os

    state_db = tmp_path / "state.db"
    state = _queue_dispatch_fixture(
        state_db,
        _event(
            source_id="AGENTS-999",
            tier="critical",
            title="Production credential change",
            requires_mj_review=True,
            gate="human",
            classifier_uncertain=True,
        )["payload"],
    )
    calls = []

    def fake_run(argv, **kwargs):  # noqa: ANN001
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = _parse_cortex_args(tmp_path, state_db, "--gateway-health-ok", "--telegram-push")

    rc = cmd_team_os(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dispatched"] == 1
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-172")
    assert row is not None
    assert row["state"] == "mj_review"
    assert row["escalation_required"] is True
    assert row["last_error"] == "human gate required"
    assert len(calls) == 2
    assert calls[0][0:2] == [__import__('sys').executable, "-c"]
    assert calls[0][-2] == "AGENTS-999"
    assert calls[1][:4] == ["hermes", "send", "--to", "telegram"]
    assert "AGENTS-999" in calls[1][4]
    assert "https://linear.app/blazeragency/issue/agents-999" in calls[1][4]


def test_cortex_cli_low_cost_telegram_flag_does_not_call_assignment_or_send(tmp_path, capsys, monkeypatch):
    import subprocess

    from hermes_cli.team_os.cli import cmd_team_os

    state_db = tmp_path / "state.db"
    _queue_dispatch_fixture(state_db, _event()["payload"])
    calls = []

    def fake_run(argv, **kwargs):  # noqa: ANN001
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = _parse_cortex_args(tmp_path, state_db, "--gateway-health-ok", "--telegram-push")

    rc = cmd_team_os(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dispatched"] == 1
    assert calls == []
