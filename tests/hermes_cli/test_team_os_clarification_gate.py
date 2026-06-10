from __future__ import annotations

from pathlib import Path


def _event(body: str, **payload_overrides):
    payload = {
        "source_id": "AGENTS-X",
        "title": "Ambiguous card",
        "body": body,
        "labels": ["system:hermes", "type:code", "failure-cost:low"],
        "failure_cost_tier": "low",
        "requires_mj_review": False,
        "url": "https://linear.app/blazeragency/issue/AGENTS-X/test",
    }
    payload.update(payload_overrides)
    return {"id": 1, "event_type": "linear_observation", "source_id": payload["source_id"], "source": "linear", "payload": payload, "state": "queued"}


def _cfg(tmp_path):
    from hermes_cli.team_os.dispatcher import DispatcherConfig

    return DispatcherConfig(repo_root=tmp_path / "repo", worktree_root=tmp_path / "workers", artifact_root=tmp_path / "artifacts", lease_root=tmp_path / "leases")


def test_clarification_gate_blocks_sparse_card_before_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import dispatch_outbox_event

    called = {"worker": 0}

    def worker(**_kwargs):
        called["worker"] += 1
        raise AssertionError("Worker must not run for clarification-needed cards")

    result = dispatch_outbox_event(_event("TBD"), _cfg(tmp_path), worker=worker)

    assert result["status"] == "clarification_needed"
    assert result["lane"] == "blocked"
    assert called["worker"] == 0
    assert result["structured_asks"]
    assert "No Worker was started" in result["clarification_card"]


def test_clarification_gate_blocks_explicit_clarification_label_before_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import dispatch_outbox_event

    calls = {"worker": 0}

    def worker(**_kwargs):
        calls["worker"] += 1
        raise AssertionError("Worker must not run")

    result = dispatch_outbox_event(
        _event(
            "Detailed enough body but this card is explicitly asking for clarification before implementation.",
            labels=["system:hermes", "type:code", "failure-cost:low", "gate:clarification"],
        ),
        _cfg(tmp_path),
        worker=worker,
    )

    assert result["status"] == "clarification_needed"
    assert "clarification gate requested" in result["reason"]
    assert calls == {"worker": 0}


def test_clarification_gate_allows_clear_low_cost_card_to_worker(tmp_path):
    from hermes_cli.team_os.dispatcher import dispatch_outbox_event

    def worker(*, contract, worktree_root, branch, **_kwargs):
        return {
            "worker_status": "completed",
            "source_ticket": contract["source_ticket"],
            "worktree_path": str(worktree_root / branch),
            "changed_files": ["pkg/clear.py"],
            "proof_results": [{"command": "python -m py_compile pkg/clear.py", "exit_code": 0}],
            "worker_output": "clear",
            "human_gate_required": True,
            "loop_feed_allowed": False,
            "auto_dispatch_allowed": False,
            "auto_done_allowed": False,
        }

    def validator(**_kwargs):
        return {"verdict": "PASS", "source_ticket": "AGENTS-X", "review_text": "VERDICT: PASS"}

    event = _event(
        "Implement a focused internal parser guard with tests and no external side effects.",
        validation_contract={
            "source_ticket": "AGENTS-X",
            "intended_behavior": "Implement clear parser guard",
            "non_goals": ["Do not touch production"],
            "assertions": ["Proof passes"],
            "commands": ["python -m py_compile pkg/clear.py"],
            "required_commands": ["python -m py_compile pkg/clear.py"],
            "behavior_check_required": True,
            "risk": "low",
            "human_gate_required": True,
            "bounce_conditions": ["Validator returns BOUNCE"],
            "files_to_touch": ["pkg/clear.py"],
        },
    )

    result = dispatch_outbox_event(event, _cfg(tmp_path), worker=worker, validator=validator)

    assert result["status"] == "validated"
