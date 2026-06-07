from __future__ import annotations

import json
from pathlib import Path


def _event(source_id: str = "AGENTS-172", *, tier: str = "low", title: str = "Planner polish") -> dict:
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
            "requires_mj_review": False,
            "failure_cost_reason": f"test maps to {tier}",
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


def test_dispatcher_runs_low_cost_outbox_event_through_worker_validator_and_telegram(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    calls: dict[str, object] = {"telegram": []}

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
    assert result["telegram_push"]["sent"] is True
    assert "AGENTS-172" in calls["telegram"][0]  # type: ignore[index]
    assert Path(result["contract_path"]).exists()
    assert Path(result["handoff_path"]).exists()


def test_dispatcher_blocks_medium_or_client_prod_money_surfaces_before_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig, dispatch_outbox_event

    def worker(**_kwargs):  # noqa: ANN003
        raise AssertionError("worker must not run")

    medium = dispatch_outbox_event(
        _event(tier="medium"),
        DispatcherConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "workers", artifact_root=tmp_path / "artifacts", lease_root=tmp_path / "leases"),
        worker=worker,
    )
    assert medium["status"] == "blocked_failure_cost"
    assert "low" in medium["reason"]

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


def test_cortex_cli_wires_dispatcher_when_live_dispatch_enabled(tmp_path, capsys):
    import argparse

    from hermes_cli.team_os.cli import cmd_team_os, register_cli
    from hermes_cli.team_os.db import TeamOSState

    state_db = tmp_path / "state.db"
    state = TeamOSState(state_db)
    state.queue_for_dispatch(
        event_type="linear_observation",
        source_id="AGENTS-172",
        source="linear",
        payload=_event()["payload"],
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    team_os = sub.add_parser("team-os")
    register_cli(team_os)
    args = parser.parse_args([
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
    ])

    rc = cmd_team_os(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dispatched"] == 1
    row = state.get_outbox_event_by_source("linear_observation", "AGENTS-172")
    assert row is not None
    assert row["state"] == "succeeded"
