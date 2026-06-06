"""Regression tests for gateway per-turn wall-clock deadline helpers."""

from gateway import run as gateway_run


def test_wall_clock_deadline_defaults_to_ten_minutes(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)

    assert gateway_run._gateway_turn_wall_clock_timeout() == 600.0


def test_wall_clock_deadline_zero_disables(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "0")

    assert gateway_run._gateway_turn_wall_clock_timeout() is None


def test_wall_clock_deadline_trips_even_when_agent_is_active(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "5")

    start_time = 100.0
    now = 106.0

    assert gateway_run._gateway_turn_wall_clock_expired(start_time, now=now) is True


def test_wall_clock_deadline_not_expired_before_limit(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "5")

    assert gateway_run._gateway_turn_wall_clock_expired(100.0, now=104.9) is False
