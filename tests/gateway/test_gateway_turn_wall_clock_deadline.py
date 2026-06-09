"""Regression tests for gateway per-turn wall-clock deadline helpers."""

from gateway import run as gateway_run


def test_wall_clock_deadline_defaults_to_ten_minutes(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_gateway_config_wall_clock_timeout",
        lambda: (False, None),
    )
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)

    assert gateway_run._gateway_turn_wall_clock_timeout() == 600.0


def test_wall_clock_deadline_zero_disables(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_gateway_config_wall_clock_timeout",
        lambda: (False, None),
    )
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "0")

    assert gateway_run._gateway_turn_wall_clock_timeout() is None


def test_wall_clock_deadline_trips_even_when_agent_is_active(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_gateway_config_wall_clock_timeout",
        lambda: (False, None),
    )
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "5")

    start_time = 100.0
    now = 106.0

    assert gateway_run._gateway_turn_wall_clock_expired(start_time, now=now) is True


def test_wall_clock_deadline_not_expired_before_limit(monkeypatch):
    monkeypatch.setattr(
        gateway_run,
        "_gateway_config_wall_clock_timeout",
        lambda: (False, None),
    )
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "5")

    assert gateway_run._gateway_turn_wall_clock_expired(100.0, now=104.9) is False


def test_gateway_runner_tool_subprocess_cleanup_reuses_shutdown_sweep(monkeypatch):
    calls = []

    class FakeRegistry:
        def kill_all(self):
            calls.append("kill_all")
            return 1

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.terminal_tool.cleanup_all_environments",
        lambda: calls.append("cleanup_all_environments"),
    )
    monkeypatch.setattr(
        "tools.browser_tool.cleanup_all_browsers",
        lambda: calls.append("cleanup_all_browsers"),
    )

    runner = object.__new__(gateway_run.GatewayRunner)

    runner._kill_tool_subprocesses("test")

    assert calls == ["kill_all", "cleanup_all_environments", "cleanup_all_browsers"]


def test_wall_clock_deadline_path_kills_tool_subprocesses_before_cancelling_executor():
    with open(gateway_run.__file__, encoding="utf-8") as handle:
        source = handle.read()
    deadline_block = source.split("if _wall_clock_timeout_fired:", 1)[1].split(
        "elif _inactivity_timeout:", 1
    )[0]

    cleanup_index = deadline_block.index('self._kill_tool_subprocesses("wall-clock-deadline")')
    cancel_index = deadline_block.index("_executor_task.cancel()")

    assert cleanup_index < cancel_index
