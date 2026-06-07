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


def test_build_durable_artifact_paths_uses_standard_names(tmp_path):
    from hermes_cli.team_os.thin_loop import build_durable_artifact_paths

    paths = build_durable_artifact_paths(
        artifact_root=tmp_path / "artifacts",
        source_ticket="AGENTS-184-PROOF 2",
        run_id="clean-run-1",
    )

    assert paths["artifact_dir"].endswith("agents-184-proof-2/clean-run-1")
    assert paths["grounding_path"].endswith("01-grounding.json")
    assert paths["contract_path"].endswith("02-contract.json")
    assert paths["mission_prompt_path"].endswith("03-developer-mission.md")
    assert paths["handoff_path"].endswith("04-worker-handoff.json")
    assert paths["validator_bounce_path"].endswith("05-validator-bounce.json")
    assert paths["adversarial_review_path"].endswith("06-adversarial-review.json")
    assert paths["validator_pass_path"].endswith("07-validator-pass.json")
    assert paths["proof_ping_path"].endswith("08-proof-ping.md")
    assert paths["manifest_path"].endswith("manifest.json")


def test_standard_engine_orchestration_writes_durable_bounce_pass_manifest(tmp_path):
    from hermes_cli.team_os.thin_loop import orchestrate_standard_engine_proof

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "hermes_cli/team_os/planner_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("def criteria():\n    return ['old']\n", encoding="utf-8")
    test_file = repo / "tests/hermes_cli/test_team_os_planner_runner.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed planner"], cwd=repo, check=True, capture_output=True)

    artifact_root = tmp_path / "artifacts"
    paths = {
        "handoff": artifact_root / "agents-184-proof-2" / "clean-run-1" / "04-worker-handoff.json",
        "adversarial": artifact_root / "agents-184-proof-2" / "clean-run-1" / "06-adversarial-review.json",
    }
    helper = tmp_path / "worker.py"
    helper.write_text(
        "import json, pathlib, subprocess\n"
        "root = pathlib.Path.cwd()\n"
        "(root / 'hermes_cli/team_os/planner_runner.py').write_text(\"def criteria():\\n    return ['Pass/fail: standard engine artifact']\\n\")\n"
        "pathlib.Path(r'%s').write_text(json.dumps({\"worker_status\":\"completed\",\"proof_output\":\"1 passed\",\"claims\":[{\"claim\":\"Standard engine changed criteria text\",\"diff_substrings\":[\"Pass/fail: standard engine artifact\"]}]}))\n"
        % paths["handoff"],
        encoding="utf-8",
    )
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(
        "import json; print(json.dumps({'verdict':'PASS','semantic_claims_supported':True,'model':'claude-max'}))\n",
        encoding="utf-8",
    )

    result = orchestrate_standard_engine_proof(
        repo_root=repo,
        artifact_root=artifact_root,
        worktree_root=tmp_path / "worktrees",
        source_ticket="AGENTS-184-PROOF 2",
        title="Prove durable artifacts",
        task_description="Change planner criteria for durable artifact proof",
        allowed_files=["hermes_cli/team_os/planner_runner.py"],
        required_commands=["python3.13 -m pytest -q tests/hermes_cli/test_team_os_planner_runner.py"],
        run_id="clean-run-1",
        worker_command=["python3.13", str(helper)],
        adversarial_command=["python3.13", str(reviewer)],
    )

    assert result["status"] == "validated"
    assert result["standard_engine"]["orchestration"] == "grounding->contract->mission->worker->planted-bounce->adversarial->validator"
    assert result["bounce"]["verdict"] == "BOUNCE"
    assert result["pass"]["verdict"] == "PASS"
    manifest = json.loads(Path(result["paths"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "validated"
    assert manifest["human_gate_required"] is True
    assert manifest["auto_done_allowed"] is False
    assert manifest["live_dispatch_allowed"] is False
    assert Path(result["paths"]["grounding_path"]).exists()
    assert Path(result["paths"]["contract_path"]).exists()
    assert Path(result["paths"]["proof_ping_path"]).read_text(encoding="utf-8").startswith("AGENTS-184-PROOF 2 thin-loop proof: PASS")


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


def test_validate_worker_handoff_bounces_claims_without_git_diff_quotes(tmp_path):
    from hermes_cli.team_os.thin_loop import validate_worker_handoff

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "hermes_cli/team_os/planner_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("def old():\n    return 'old'\n", encoding="utf-8")
    test_file = repo / "tests/hermes_cli/test_team_os_planner_runner.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed planner"], cwd=repo, check=True, capture_output=True)
    source.write_text("def old():\n    return 'Pass/fail:'\n", encoding="utf-8")

    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "worker_status": "completed",
                "proof_output": "pytest passed",
                "claims": [{"claim": "Generated criteria now use Pass/fail labels"}],
            }
        ),
        encoding="utf-8",
    )

    result = validate_worker_handoff(
        contract_path=contract,
        worktree_path=repo,
        handoff_path=handoff,
        output_path=tmp_path / "bounce.json",
    )

    assert result["verdict"] == "BOUNCE"
    assert any("diff_substrings" in err for err in result["errors"])


def test_validate_worker_handoff_passes_only_with_quoted_git_diff_evidence(tmp_path):
    from hermes_cli.team_os.thin_loop import validate_worker_handoff

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "hermes_cli/team_os/planner_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("def criteria():\n    return ['complete the subtask']\n", encoding="utf-8")
    test_file = repo / "tests/hermes_cli/test_team_os_planner_runner.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed planner"], cwd=repo, check=True, capture_output=True)
    source.write_text("def criteria():\n    return ['Pass/fail: observable behavior']\n", encoding="utf-8")
    test_file.write_text("def test_crisp():\n    assert 'Pass/fail:'\n", encoding="utf-8")

    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "worker_status": "completed",
                "proof_output": "2 passed",
                "claims": [
                    {
                        "claim": "Generated criteria now use Pass/fail labels",
                        "diff_substrings": ["Pass/fail: observable behavior"],
                    },
                    {
                        "claim": "Focused test covers crisp criteria",
                        "diff_substrings": ["def test_crisp"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    adversarial = tmp_path / "adversarial.json"
    adversarial.write_text(
        json.dumps({"verdict": "PASS", "semantic_claims_supported": True, "model": "claude-max"}),
        encoding="utf-8",
    )
    result = validate_worker_handoff(
        contract_path=contract,
        worktree_path=repo,
        handoff_path=handoff,
        adversarial_review_path=adversarial,
        output_path=tmp_path / "pass.json",
    )

    assert result["verdict"] == "PASS"
    assert result["errors"] == []
    quoted = "\n".join(line for item in result["diff_quotes"] for line in item["diff_lines"])
    assert "+    return ['Pass/fail: observable behavior']" in quoted
    assert "+def test_crisp" in quoted
    assert result["auto_done_allowed"] is False


def test_validate_worker_handoff_uses_diff_head_for_committed_worker_changes(tmp_path):
    from hermes_cli.team_os.thin_loop import validate_worker_handoff

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "hermes_cli/team_os/planner_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("def criteria():\n    return ['complete the subtask']\n", encoding="utf-8")
    test_file = repo / "tests/hermes_cli/test_team_os_planner_runner.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed planner"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    source.write_text("def criteria():\n    return ['Pass/fail: committed behavior']\n", encoding="utf-8")
    subprocess.run(["git", "add", "hermes_cli/team_os/planner_runner.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "worker commit"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "reset", "--soft", base], cwd=repo, check=True)

    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "worker_status": "completed",
                "proof_output": "1 passed",
                "claims": [
                    {
                        "claim": "Committed worker changes remain visible to Validator",
                        "diff_substrings": ["Pass/fail: committed behavior"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    adversarial = tmp_path / "adversarial.json"
    adversarial.write_text(
        json.dumps({"verdict": "PASS", "semantic_claims_supported": True, "model": "claude-max"}),
        encoding="utf-8",
    )
    result = validate_worker_handoff(
        contract_path=contract,
        worktree_path=repo,
        handoff_path=handoff,
        adversarial_review_path=adversarial,
    )

    assert result["verdict"] == "PASS"
    assert result["changed_files"] == ["hermes_cli/team_os/planner_runner.py"]


def test_validate_worker_handoff_requires_adversarial_semantic_pass(tmp_path):
    from hermes_cli.team_os.thin_loop import validate_worker_handoff

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "hermes_cli/team_os/planner_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("def criteria():\n    return ['old']\n", encoding="utf-8")
    test_file = repo / "tests/hermes_cli/test_team_os_planner_runner.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed planner"], cwd=repo, check=True, capture_output=True)
    source.write_text("def criteria():\n    return ['Pass/fail: observable behavior']\n", encoding="utf-8")

    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "worker_status": "completed",
                "proof_output": "1 passed",
                "claims": [
                    {
                        "claim": "Gateway media placeholder bug is fixed",
                        "diff_substrings": ["Pass/fail: observable behavior"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    missing = validate_worker_handoff(
        contract_path=contract,
        worktree_path=repo,
        handoff_path=handoff,
    )

    assert missing["verdict"] == "BOUNCE"
    assert any("adversarial" in err.lower() for err in missing["errors"])

    adversarial = tmp_path / "adversarial.json"
    adversarial.write_text(
        json.dumps(
            {
                "verdict": "BOUNCE",
                "semantic_claims_supported": False,
                "model": "claude-max",
                "findings": ["Diff only changes generic criteria text; it does not support a gateway media placeholder fix claim."],
            }
        ),
        encoding="utf-8",
    )

    bounced = validate_worker_handoff(
        contract_path=contract,
        worktree_path=repo,
        handoff_path=handoff,
        adversarial_review_path=adversarial,
    )

    assert bounced["verdict"] == "BOUNCE"
    assert any("semantic" in err.lower() for err in bounced["errors"])


def test_run_adversarial_validator_uses_claude_max_cold_session_and_parses_wrapper_json(tmp_path):
    from hermes_cli.team_os.thin_loop import build_adversarial_validator_prompt, run_adversarial_validator

    contract = tmp_path / "contract.json"
    handoff = tmp_path / "handoff.json"
    review = tmp_path / "adversarial.json"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff.write_text(json.dumps({"claims": []}), encoding="utf-8")

    prompt = build_adversarial_validator_prompt(
        contract_path=contract,
        worktree_path=worktree,
        handoff_path=handoff,
    )
    assert "Claude Max" in prompt
    assert "different model than the Worker" in prompt
    assert "semantic" in prompt.lower()
    assert "diff actually supports each claim" in prompt

    helper = tmp_path / "reviewer.py"
    helper.write_text(
        "import json; print(json.dumps({'type':'result','result': json.dumps({'verdict':'PASS','semantic_claims_supported': True, 'model':'claude-max'})}))",
        encoding="utf-8",
    )

    result = run_adversarial_validator(
        contract_path=contract,
        worktree_path=worktree,
        handoff_path=handoff,
        output_path=review,
        command=["python3.13", str(helper)],
    )

    assert result["ok"] is True
    assert result["review"]["verdict"] == "PASS"
    assert result["review"]["semantic_claims_supported"] is True
    assert json.loads(review.read_text(encoding="utf-8"))["model"] == "claude-max"


def test_run_adversarial_validator_extracts_json_from_claude_markdown_result(tmp_path):
    from hermes_cli.team_os.thin_loop import run_adversarial_validator

    contract = tmp_path / "contract.json"
    handoff = tmp_path / "handoff.json"
    review = tmp_path / "adversarial.json"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    contract.write_text(json.dumps(_contract()), encoding="utf-8")
    handoff.write_text(json.dumps({"claims": []}), encoding="utf-8")
    helper = tmp_path / "reviewer.py"
    helper.write_text(
        "import json\n"
        "payload = {'type': 'result', 'result': 'Review done.\\n```json\\n{\\\"verdict\\\":\\\"PASS\\\",\\\"semantic_claims_supported\\\":true,\\\"model\\\":\\\"claude-max\\\"}\\n```'}\n"
        "print(json.dumps(payload))\n",
        encoding="utf-8",
    )

    result = run_adversarial_validator(
        contract_path=contract,
        worktree_path=worktree,
        handoff_path=handoff,
        output_path=review,
        command=["python3.13", str(helper)],
    )

    assert result["ok"] is True
    assert result["review"]["model"] == "claude-max"
    assert result["review"]["verdict"] == "PASS"


def test_render_proof_ping_is_blocked_until_validator_pass():
    from hermes_cli.team_os.thin_loop import render_proof_ping

    bounce = {"verdict": "BOUNCE"}
    not_passed = {"verdict": "BOUNCE"}

    try:
        render_proof_ping(source_ticket="AGENTS-172", bounce=bounce, passed=not_passed, commits=[])
    except ValueError as exc:
        assert "PASS" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("proof ping should require Validator PASS")


def test_render_proof_ping_includes_bounce_pass_diff_quotes_and_commits():
    from hermes_cli.team_os.thin_loop import render_proof_ping

    message = render_proof_ping(
        source_ticket="AGENTS-172",
        bounce={"verdict": "BOUNCE"},
        passed={
            "verdict": "PASS",
            "changed_files": ["hermes_cli/team_os/planner_runner.py"],
            "auto_done_allowed": False,
            "diff_quotes": [
                {
                    "claim": "criteria are crisp",
                    "diff_lines": ["+        f\"Pass/fail: {subject} is satisfied\""],
                }
            ],
        },
        commits=["abc123 [verified] example"],
    )

    assert "AGENTS-172 thin-loop proof: PASS" in message
    assert "planted_bounce: BOUNCE" in message
    assert "corrected_pass: PASS" in message
    assert "+        f\"Pass/fail: {subject} is satisfied\"" in message
    assert "abc123 [verified] example" in message
    assert "auto_done_allowed: false" in message
