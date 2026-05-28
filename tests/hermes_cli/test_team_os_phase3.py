import json


def test_verification_plan_lints_changed_python_and_runs_focused_tests():
    from hermes_cli.team_os.verification_gate import build_verification_plan

    plan = build_verification_plan(
        task_id="AGENTS-70",
        changed_files=["hermes_cli/team_os/verification_gate.py", "README.md"],
        focused_tests=["tests/hermes_cli/test_team_os_phase3.py"],
    )

    command_names = [command.name for command in plan.commands]
    assert command_names == ["syntax", "lint", "focused-tests"]
    assert plan.requires_full_smoke is False
    assert plan.commands[0].argv[-1] == "hermes_cli/team_os/verification_gate.py"
    assert plan.commands[1].argv == ("ruff", "check", "hermes_cli/team_os/verification_gate.py")
    assert plan.commands[2].argv[-1] == "tests/hermes_cli/test_team_os_phase3.py"


def test_verification_plan_adds_smoke_for_gateway_or_runtime_changes():
    from hermes_cli.team_os.verification_gate import build_verification_plan

    plan = build_verification_plan(
        task_id="AGENTS-70",
        changed_files=["gateway/run.py", "agent/current_work.py"],
        focused_tests=[],
    )

    command_names = [command.name for command in plan.commands]
    assert "runtime-smoke" in command_names
    smoke = next(command for command in plan.commands if command.name == "runtime-smoke")
    assert smoke.argv[-2:] == ("tests/test_current_work.py", "tests/gateway/test_unknown_command.py")
    assert plan.requires_full_smoke is True


def test_failing_verification_result_cannot_close_task():
    from hermes_cli.team_os.verification_gate import CommandResult, VerificationReport, VerificationStatus

    report = VerificationReport(
        task_id="AGENTS-70",
        status=VerificationStatus.FAILED,
        can_close=False,
        commands=(
            CommandResult(name="lint", argv=("ruff", "check", "."), exit_code=1, output="lint failed"),
        ),
        proof_artifact=None,
    )

    assert report.can_close is False
    assert report.status is VerificationStatus.FAILED
    assert report.to_dict()["can_close"] is False


def test_passing_verification_report_writes_proof_artifact(tmp_path):
    from hermes_cli.team_os.verification_gate import CommandResult, VerificationReport, VerificationStatus, write_proof_artifact

    report = VerificationReport(
        task_id="AGENTS-70",
        status=VerificationStatus.PASSED,
        can_close=True,
        commands=(
            CommandResult(name="lint", argv=("ruff", "check", "file.py"), exit_code=0, output="ok"),
        ),
        proof_artifact=None,
    )

    output = write_proof_artifact(report, tmp_path / "proof.json")

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_id"] == "AGENTS-70"
    assert data["status"] == "passed"
    assert data["can_close"] is True
    assert data["commands"][0]["name"] == "lint"
