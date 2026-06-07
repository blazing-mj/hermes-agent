"""Thin Team OS execution loop tests for AGENTS-172 proof plumbing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _contract():
    from hermes_cli.team_os.thin_loop import build_thin_contract

    return build_thin_contract(
        source_ticket="AGENTS-172",
        title="Polish Planner acceptance criteria into crisp testable conditions",
        allowed_files=[
            "hermes_cli/team_os/planner_runner.py",
            "tests/hermes_cli/test_team_os_planner_runner.py",
        ],
        required_commands=[
            "python3.13 -m pytest -o addopts='' tests/hermes_cli/test_team_os_planner_runner.py -q",
        ],
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def test_prepare_thin_loop_mission_writes_contract_lease_status_and_worktree(tmp_path):
    from hermes_cli.team_os.thin_loop import prepare_thin_loop_mission

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    result = prepare_thin_loop_mission(
        repo_root=repo,
        state_root=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        contract=_contract(),
        run_id="proof1",
    )

    assert result["ok"] is True
    paths = result["paths"]
    assert json.loads(Path(paths["contract_path"]).read_text())["source_ticket"] == "AGENTS-172"
    assert json.loads(Path(paths["lease_path"]).read_text())["owner"] == "team-os-thin-loop"
    status = json.loads(Path(paths["status_path"]).read_text())
    assert status["status"] == "prepared"
    assert status["worktree_created"] is True
    assert status["auto_done_allowed"] is False
    assert Path(paths["worktree_path"]).is_dir()


def test_prepare_thin_loop_mission_denies_risky_surface_before_worktree(tmp_path):
    from hermes_cli.team_os.thin_loop import prepare_thin_loop_mission

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    contract = _contract()
    contract["files_to_touch"] = ["prod/.env"]

    result = prepare_thin_loop_mission(
        repo_root=repo,
        state_root=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        contract=contract,
        run_id="denied",
    )

    assert result["ok"] is False
    assert result["status"] == "denied"
    assert "denied surface" in result["violations"][0]
    assert not Path(result["paths"]["worktree_path"]).exists()
    status = json.loads(Path(result["paths"]["status_path"]).read_text())
    assert status["worktree_created"] is False


def test_exec_slice_uses_teamos_exec_prompt_and_requires_handoff_file(tmp_path):
    from hermes_cli.team_os.thin_loop import build_developer_mission_prompt, run_teamos_exec_slice

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    contract = tmp_path / "contract.json"
    handoff = tmp_path / "handoff.json"
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    helper = tmp_path / "write_handoff.py"
    helper.write_text(
        "import json, pathlib; pathlib.Path(r'%s').write_text(json.dumps({'worker_status':'completed'}))\n" % handoff,
        encoding="utf-8",
    )

    prompt = build_developer_mission_prompt(
        contract_path=contract,
        worktree_path=worktree,
        handoff_path=handoff,
    )
    assert "teamos-exec" in prompt
    assert "delegate_task" in prompt
    assert str(worktree) in prompt
    assert "Do not merge" in prompt

    result = run_teamos_exec_slice(
        contract_path=contract,
        worktree_path=worktree,
        handoff_path=handoff,
        mission_prompt_path=tmp_path / "mission_prompt.md",
        command=["python3.13", str(helper)],
    )

    assert result["ok"] is True
    assert result["profile"] == "teamos-exec"
    assert Path(result["mission_prompt_path"]).exists()
    assert json.loads(handoff.read_text())["worker_status"] == "completed"
