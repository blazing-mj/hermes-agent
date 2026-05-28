def test_loop_runner_filters_shift_and_selects_highest_priority():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(task_id="night", title="night task", priority=100, shifts=("night",), quota_confidence="high"),
        LoopTask(task_id="day-low", title="day low", priority=1, shifts=("day",), quota_confidence="high"),
        LoopTask(task_id="day-high", title="day high", priority=10, shifts=("day",), quota_confidence="high"),
    ]

    decision = select_next_task(tasks, current_shift="day")

    assert decision.selected_task_id == "day-high"
    assert decision.dry_run is True
    assert decision.would_spawn_worker is False
    assert "night" in decision.skipped_task_ids


def test_loop_runner_skips_blocked_approval_and_low_or_unknown_quota():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(task_id="blocked", title="approval blocked", priority=100, approval_status="pending"),
        LoopTask(task_id="quota-low", title="quota low", priority=90, quota_confidence="low"),
        LoopTask(task_id="quota-unknown", title="quota unknown", priority=80, quota_confidence="unknown"),
        LoopTask(task_id="ready", title="ready", priority=1, quota_confidence="high"),
    ]

    decision = select_next_task(tasks, current_shift="day")

    assert decision.selected_task_id == "ready"
    assert decision.skip_reasons["blocked"] == "approval pending"
    assert decision.skip_reasons["quota-low"] == "quota confidence low"
    assert decision.skip_reasons["quota-unknown"] == "quota confidence unknown"


def test_loop_runner_logs_decision_without_spawning(tmp_path):
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task, write_loop_decision

    decision = select_next_task([LoopTask(task_id="ready", title="ready", priority=1, quota_confidence="high")], current_shift="day")
    output = write_loop_decision(decision, tmp_path / "loop-decision.json")

    assert output.read_text(encoding="utf-8").count("ready") >= 1
    assert '"dry_run": true' in output.read_text(encoding="utf-8")
    assert '"would_spawn_worker": false' in output.read_text(encoding="utf-8")


def test_loop_runner_lock_blocks_duplicate_runner(tmp_path):
    from hermes_cli.team_os.loop_runner import RunnerAlreadyActive, acquire_runner_lock

    lock_path = tmp_path / "loop.lock"
    first = acquire_runner_lock(lock_path, owner="runner-a")

    assert first.exists()
    try:
        try:
            acquire_runner_lock(lock_path, owner="runner-b")
        except RunnerAlreadyActive as exc:
            assert "runner-a" in str(exc)
        else:
            raise AssertionError("duplicate runner was not blocked")
    finally:
        first.release()

    assert not lock_path.exists()
