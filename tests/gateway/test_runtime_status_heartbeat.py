"""Tests for gateway runtime status heartbeat semantics."""

import asyncio

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_runtime_status_heartbeat_updates_while_agent_active(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {"telegram:chat:user": object()}
    updates = []
    sleeps = {"count": 0}
    real_sleep = asyncio.sleep

    def fake_update(state=None, exit_reason=None):
        updates.append((state, exit_reason))

    async def fake_sleep(_seconds):
        sleeps["count"] += 1
        if sleeps["count"] >= 2:
            runner._running = False
        await real_sleep(0)

    runner._update_runtime_status = fake_update
    monkeypatch.setattr("gateway.run.asyncio.sleep", fake_sleep)

    await runner._runtime_status_heartbeat(interval=0.01)

    assert updates == [("running", None), ("running", None)]
