from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli.team_os.integrator import (
    AutoLandCounter,
    IntegratorInput,
    build_gate_card,
    classify_integrator_action,
    integrate_after_validator,
)


def _handoff(tmp_path: Path, **updates) -> Path:
    payload = {
        "linear_issue": "AGENTS-999",
        "summary": "Docs-only reversible fix",
        "changed_files": ["docs/team-os/example.md"],
        "rollback_commands": ["git revert abc123 --no-edit"],
        "risk": "low",
        "side_effects": [],
        "plain_language": {
            "decision": "Approve the docs-only change.",
            "problem": "The docs were stale.",
            "what_changed": "The wording now matches runtime behavior.",
            "how_it_behaves_now": "Operators see the current command.",
            "approving": "Allow this docs-only change to land.",
            "not_approving": "No production restart, send, spend, credential, delete, or customer action.",
            "rollback": "Revert the docs-only change.",
            "proof": "Validator PASS and tests are linked on the ticket.",
        },
    }
    payload.update(updates)
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _validator(tmp_path: Path, verdict: str = "PASS") -> Path:
    path = tmp_path / "validator.json"
    path.write_text(json.dumps({"verdict": verdict, "model": "claude-max"}), encoding="utf-8")
    return path


def test_reversible_validator_pass_lands_locally_without_push_or_deploy_by_default(tmp_path: Path):
    calls: list[list[str]] = []
    comments: list[str] = []
    pings: list[str] = []

    result = integrate_after_validator(
        IntegratorInput(
            source_ticket="AGENTS-999",
            worktree_path=tmp_path,
            handoff_path=_handoff(tmp_path),
            validator_report_path=_validator(tmp_path),
            main_branch="main",
            deploy_command=["hermes", "gateway", "restart"],
            fyi_counter_path=tmp_path / "fyi.json",
            fyi_limit=3,
        ),
        runner=lambda argv, cwd: calls.append(list(argv)) or "ok",
        linear_commenter=lambda issue, body: comments.append(f"{issue}:{body}"),
        fyi_sender=lambda body: pings.append(body),
    )

    assert result.status == "auto_landed"
    assert result.reversibility == "reversible"
    assert [c[0:2] for c in calls] == [["git", "branch"], ["git", "fetch"], ["git", "checkout"], ["git", "merge"]]
    assert calls[3] == ["git", "merge", "--ff-only", "ok"]
    assert all(call[:2] != ["git", "push"] for call in calls)
    assert all(call[:2] != ["hermes", "gateway"] for call in calls)
    assert "Rollback commands" in comments[-1]
    assert "git revert" in comments[-1]
    assert pings and "FYI" in pings[0]


def test_irreversible_validator_pass_stops_at_needs_mj_with_zero_code_gate(tmp_path: Path):
    comments: list[str] = []
    statuses: list[tuple[str, str]] = []
    handoff = _handoff(
        tmp_path,
        changed_files=["gateway/run.py"],
        risk="critical",
        side_effects=["restart", "credential"],
        summary="Runtime credential change",
    )

    result = integrate_after_validator(
        IntegratorInput(
            source_ticket="AGENTS-999",
            worktree_path=tmp_path,
            handoff_path=handoff,
            validator_report_path=_validator(tmp_path),
        ),
        runner=lambda argv, cwd: pytest.fail("irreversible work must not run git/deploy"),
        linear_commenter=lambda issue, body: comments.append(body),
        linear_status=lambda issue, status: statuses.append((issue, status)),
    )

    assert result.status == "needs_mj"
    assert statuses == [("AGENTS-999", "Needs-MJ")]
    gate = comments[-1]
    assert "## 🛑 What needs your decision" in gate
    assert "## ❓ The problem this solves" in gate
    assert "## 🔧 What was changed" in gate
    assert "## ▶️ How it behaves AFTER you approve" in gate
    assert "## ✅ What you are approving" in gate
    assert "## 🚫 What you are NOT approving" in gate
    assert "## ↩️ If it goes wrong" in gate
    assert "## 🔍 Proof it works (for the record, not for you to read)" in gate
    assert "```" not in gate
    assert "gateway/run.py" not in gate


def test_validator_bounce_never_lands(tmp_path: Path):
    result = integrate_after_validator(
        IntegratorInput(
            source_ticket="AGENTS-999",
            worktree_path=tmp_path,
            handoff_path=_handoff(tmp_path),
            validator_report_path=_validator(tmp_path, "BOUNCE"),
        ),
        runner=lambda argv, cwd: pytest.fail("BOUNCE must not run git/deploy"),
    )
    assert result.status == "blocked"
    assert "Validator verdict is BOUNCE" in result.reason


def test_auto_land_still_returns_rollback_if_linear_comment_fails(tmp_path: Path):
    result = integrate_after_validator(
        IntegratorInput(
            source_ticket="AGENTS-999",
            worktree_path=tmp_path,
            handoff_path=_handoff(tmp_path),
            validator_report_path=_validator(tmp_path),
        ),
        runner=lambda argv, cwd: "feature-branch" if argv[:2] == ("git", "branch") else "ok",
        linear_commenter=lambda issue, body: (_ for _ in ()).throw(RuntimeError("linear down")),
    )

    assert result.status == "auto_landed"
    assert result.rollback_commands == ("git revert abc123 --no-edit",)
    assert "linear comment failed" in result.reason


def test_auto_land_still_returns_rollback_if_fyi_fails(tmp_path: Path):
    result = integrate_after_validator(
        IntegratorInput(
            source_ticket="AGENTS-999",
            worktree_path=tmp_path,
            handoff_path=_handoff(tmp_path),
            validator_report_path=_validator(tmp_path),
            fyi_counter_path=tmp_path / "fyi.json",
        ),
        runner=lambda argv, cwd: "feature-branch" if argv[:2] == ("git", "branch") else "ok",
        fyi_sender=lambda body: (_ for _ in ()).throw(RuntimeError("telegram down")),
    )

    assert result.status == "auto_landed"
    assert result.rollback_commands == ("git revert abc123 --no-edit",)
    assert "fyi failed" in result.reason


def test_first_three_fyi_pings_then_silent(tmp_path: Path):
    counter = AutoLandCounter(tmp_path / "counter.json")
    assert [counter.should_send_and_increment(limit=3) for _ in range(5)] == [True, True, True, False, False]


def test_risk_classifier_marks_restarts_credentials_deletes_prod_money_irreversible(tmp_path: Path):
    safe = json.loads(_handoff(tmp_path).read_text())
    assert classify_integrator_action(safe).reversibility == "reversible"
    for side_effect in ["money", "send", "production", "credential", "delete", "restart"]:
        payload = dict(safe, side_effects=[side_effect])
        assert classify_integrator_action(payload).reversibility == "irreversible"
    assert classify_integrator_action(dict(safe, summary="reproduce the failing test")).reversibility == "reversible"


def test_gate_card_uses_plain_language_not_code():
    card = build_gate_card(
        problem="Secret rotation is needed.",
        what_changed="The agent prepared a credential update.",
        how_it_behaves_now="Nothing changes until approval.",
        approving="Allow the credential update.",
        not_approving="No sends, deletes, spends, or restarts.",
        source_ticket="AGENTS-999",
    )
    assert "## 🛑 What needs your decision" in card
    assert "## ❓ The problem this solves" in card
    assert "## 🔧 What was changed" in card
    assert "## ▶️ How it behaves AFTER you approve" in card
    assert "## ✅ What you are approving" in card
    assert "## 🚫 What you are NOT approving" in card
    assert "## ↩️ If it goes wrong" in card
    assert "## 🔍 Proof it works (for the record, not for you to read)" in card
    assert "```" not in card
    assert "def " not in card


def test_cli_integrator_plan_only_writes_decision_without_side_effects(tmp_path: Path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "decision.json"
    args = argparse.Namespace(
        team_os_command="integrate",
        source_ticket="AGENTS-999",
        worktree=str(tmp_path),
        handoff=str(_handoff(tmp_path)),
        validator_report=str(_validator(tmp_path)),
        output=str(output),
        main_branch="main",
        deploy_command=[],
        fyi_counter=str(tmp_path / "fyi.json"),
        fyi_limit=3,
        apply=False,
    )

    rc = cmd_team_os(args)

    assert rc == 0
    data = json.loads(output.read_text())
    assert data["status"] == "planned"
    assert data["would_status"] == "auto_landed"
    assert data["reversibility"] == "reversible"
