"""Team OS Developer/Worker runner tests for AGENTS-177.

Stage 3 requires ephemeral workers: one ticket, isolated git worktree, lease,
hard timeout, mechanical write-surface boundaries, null-output fallback, and
human gate/no-auto-Done invariants.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest


class _RootParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: ANN001
        raise AssertionError(message)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "pkg").mkdir()
    (path / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "README.md").write_text("fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _contract(repo: Path) -> dict:
    return {
        "source_ticket": "AGENTS-177-DEMO",
        "problem": "Easy low-risk fixture code change",
        "files_to_touch": ["pkg/feature.py"],
        "implementation_scope": ["Change only the fixture module in the isolated worker worktree"],
        "acceptance_criteria": ["pkg.feature exposes VALUE = 2"],
        "proof_required": ["git diff --stat", "python -m py_compile pkg/feature.py"],
        "required_commands": ["python -m py_compile pkg/feature.py"],
        "intended_behavior": "Worker updates fixture code in an isolated git worktree only",
        "non_goals": ["Do not touch live gateway/runtime", "Do not edit credentials or production paths"],
        "assertions": ["Changed files are limited to pkg/feature.py", "Human gate remains required"],
        "commands": ["python -m py_compile pkg/feature.py"],
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": ["Worker touches denied paths", "Proof missing", "Human gate disabled"],
    }


def test_worker_runner_rejects_live_runtime_or_main_checkout_workspace(tmp_path):
    from hermes_cli.team_os.worker_runner import check_worker_boundary

    repo = tmp_path / "repo"
    repo.mkdir()

    violations = check_worker_boundary(
        repo_root=repo,
        worktree_path=repo,
        worktree_root=tmp_path / "workers",
        allowed_files=["pkg/feature.py"],
    )

    assert any("isolated worktree" in item.lower() for item in violations)
    assert any("outside worktree root" in item.lower() for item in violations)


def test_worker_runner_rejects_money_credentials_prod_and_runtime_paths(tmp_path):
    from hermes_cli.team_os.worker_runner import check_worker_boundary

    repo = tmp_path / "repo"
    worktree_root = tmp_path / "workers"
    worktree = worktree_root / "wt"
    repo.mkdir()
    worktree.mkdir(parents=True)

    violations = check_worker_boundary(
        repo_root=repo,
        worktree_path=worktree,
        worktree_root=worktree_root,
        allowed_files=[
            ".env",
            "config/credentials.yaml",
            "billing/money.py",
            "prod/deploy.py",
            "gateway/runtime_state.json",
            "pkg/feature.py",
        ],
    )

    joined = "\n".join(violations).lower()
    assert ".env" in joined
    assert "credentials" in joined
    assert "money" in joined
    assert "prod" in joined
    assert "gateway/runtime" in joined
    assert "pkg/feature.py" not in joined


def test_run_worker_denies_bad_contract_before_executor_or_worktree(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)
    denied = _contract(repo)
    denied["files_to_touch"] = [".env", "pkg/feature.py"]

    def executor(prompt: str, cwd: Path, timeout: float) -> str:  # noqa: ARG001
        raise AssertionError("executor must not run when boundary is denied")

    result = run_worker(
        contract=denied,
        repo_root=repo,
        worktree_root=tmp_path / "worker-worktrees",
        lease_path=tmp_path / "lease.json",
        branch="agents-177-denied",
        timeout_seconds=30,
        executor=executor,
    )

    assert result["worker_status"] == "boundary_denied"
    assert any(".env" in item for item in result["boundary_violations"])
    assert (tmp_path / "worker-worktrees" / "agents-177-denied").exists() is False


def test_run_worker_returns_structured_lease_denial_without_crashing(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)
    lease_path = tmp_path / "lease.json"
    lease_path.write_text(json.dumps({"expires_at": 4_000_000_000}), encoding="utf-8")

    result = run_worker(
        contract=_contract(repo),
        repo_root=repo,
        worktree_root=tmp_path / "worker-worktrees",
        lease_path=lease_path,
        branch="agents-177-lease-held",
        timeout_seconds=30,
        executor=lambda prompt, cwd, timeout: "should not run",
    )

    assert result["worker_status"] == "lease_denied"
    assert result["changed_files"] == []
    assert (tmp_path / "worker-worktrees" / "agents-177-lease-held").exists() is False


def test_run_worker_releases_lease_when_executor_raises(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)
    lease_path = tmp_path / "lease.json"

    def executor(prompt: str, cwd: Path, timeout: float) -> str:  # noqa: ARG001
        raise RuntimeError("boom")

    result = run_worker(
        contract=_contract(repo),
        repo_root=repo,
        worktree_root=tmp_path / "worker-worktrees",
        lease_path=lease_path,
        branch="agents-177-executor-error",
        timeout_seconds=30,
        executor=executor,
    )

    assert result["worker_status"] == "failed"
    assert "boom" in result["error"]
    assert lease_path.exists() is False


def test_run_worker_returns_structured_worktree_failure(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "branch", "agents-177-conflict"], cwd=repo, check=True)

    result = run_worker(
        contract=_contract(repo),
        repo_root=repo,
        worktree_root=tmp_path / "worker-worktrees",
        lease_path=tmp_path / "lease.json",
        branch="agents-177-conflict",
        timeout_seconds=30,
        executor=lambda prompt, cwd, timeout: "should not run",
    )

    assert result["worker_status"] == "worktree_failed"
    assert result["changed_files"] == []


def test_worker_runner_creates_isolated_worktree_and_handoff_from_executor(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree_root = tmp_path / "worker-worktrees"
    lease_path = tmp_path / "leases" / "AGENTS-177-DEMO.json"

    def executor(prompt: str, cwd: Path, timeout: float) -> str:  # noqa: ARG001
        assert cwd != repo
        assert worktree_root in cwd.parents
        (cwd / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        return "worker changed pkg/feature.py and ran proof"

    result = run_worker(
        contract=_contract(repo),
        repo_root=repo,
        worktree_root=worktree_root,
        lease_path=lease_path,
        branch="agents-177-demo",
        timeout_seconds=30,
        executor=executor,
    )

    assert result["worker_status"] == "completed"
    assert result["source_ticket"] == "AGENTS-177-DEMO"
    assert result["human_gate_required"] is True
    assert result["auto_done_allowed"] is False
    assert result["auto_dispatch_allowed"] is False
    assert result["loop_feed_allowed"] is False
    assert result["worktree_path"] != str(repo)
    assert result["worktree_path"].startswith(str(worktree_root))
    assert result["changed_files"] == ["pkg/feature.py"]
    assert result["worker_output"]
    assert result["proof_results"]
    assert result["proof_results"][0]["command"] == "python -m py_compile pkg/feature.py"
    assert result["proof_results"][0]["exit_code"] == 0
    assert lease_path.exists() is False


def test_worker_runner_null_output_guard_returns_fallback_required(tmp_path):
    from hermes_cli.team_os.worker_runner import run_worker

    repo = tmp_path / "repo"
    _init_repo(repo)

    result = run_worker(
        contract=_contract(repo),
        repo_root=repo,
        worktree_root=tmp_path / "worker-worktrees",
        lease_path=tmp_path / "lease.json",
        branch="agents-177-null-output",
        timeout_seconds=30,
        executor=lambda prompt, cwd, timeout: "   \n\t  ",
    )

    assert result["worker_status"] == "fallback_required"
    assert result["fallback_reason"] == "worker produced no output"
    assert result["human_gate_required"] is True
    assert result["auto_done_allowed"] is False
    assert result["changed_files"] == []


def test_worker_runner_cli_registered_and_writes_handoff(tmp_path):
    from hermes_cli.team_os.cli import register_cli

    repo = tmp_path / "repo"
    _init_repo(repo)
    contract = _contract(repo)
    contract["files_to_touch"] = ["gateway/runtime_state.json"]
    contract_path = tmp_path / "contract.json"
    output_path = tmp_path / "handoff.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    root = _RootParser(prog="hermes")
    team_os = root.add_subparsers(dest="command").add_parser("team-os")
    register_cli(team_os)
    args = root.parse_args(
        [
            "team-os",
            "run-worker",
            "--contract",
            str(contract_path),
            "--repo-root",
            str(repo),
            "--worktree-root",
            str(tmp_path / "workers"),
            "--lease",
            str(tmp_path / "lease.json"),
            "--branch",
            "agents-177-cli",
            "--output",
            str(output_path),
        ]
    )

    with pytest.raises(SystemExit) as exc:
        args.func(args)

    assert exc.value.code == 1
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["worker_status"] == "boundary_denied"
    assert data["human_gate_required"] is True
    assert data["auto_done_allowed"] is False
    assert "gateway/runtime" in "\n".join(data["boundary_violations"])
