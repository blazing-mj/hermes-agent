"""Dispatcher stuck-telemetry precision + fast-block of unspawnable tasks.

Two coupled fixes:

- A ``dir``-workspace task with no ``workspace_path`` can never resolve a
  workspace, so the dispatcher blocks it on the FIRST attempt (``force_block``)
  instead of slow-failing across ``failure_limit`` ticks. That stops such
  tasks lingering in ``ready`` for days.
- ``has_spawnable_ready`` (the health-telemetry probe) excludes tasks the
  dispatcher is deliberately holding via ``check_respawn_guard``, so the
  "stuck: check venv/PATH/credentials" warning only fires on genuinely
  spawnable work — without suppressing that real signal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (no board default_workdir)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_dir_task_without_path_blocks_on_first_attempt(kanban_home, all_assignees_spawnable):
    """A ``dir`` task with no workspace_path is permanently unspawnable, so the
    dispatcher fast-blocks it (1 failure) rather than slow-failing to the
    ``failure_limit`` over many ticks — the behaviour that left such tasks
    sitting in ``ready`` and tripping the false 'stuck' warning."""
    conn = kb.connect()
    try:
        # The test board has no default_workdir, so a dir task created without
        # an explicit path lands with workspace_path=None — the exact shape of
        # the real stuck tasks (t_612174 / t_6dae10).
        tid = kb.create_task(
            conn, title="malformed", assignee="worker", workspace_kind="dir"
        )
        task = kb.get_task(conn, tid)
        assert task.workspace_kind == "dir"
        assert not task.workspace_path
        assert task.status == "ready"
        assert kb.DEFAULT_FAILURE_LIMIT == 2  # proves 1 != the limit asserted below

        # spawn_fn is never reached — resolve_workspace raises ValueError first.
        res = kb.dispatch_once(conn, spawn_fn=lambda t, ws: 1)

        assert tid in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # Blocked immediately on the FIRST failure, not after failure_limit.
        assert task.consecutive_failures == 1
        assert task.last_failure_error and "workspace_kind=dir" in task.last_failure_error
    finally:
        conn.close()


def test_transient_spawn_failure_still_slow_fails(kanban_home, all_assignees_spawnable):
    """Regression guard against over-eager blocking: a *transient* spawn
    failure (worker subprocess raising) must still slow-fail through the
    circuit breaker, NOT block on the first attempt. Only permanent
    workspace-config errors fast-block."""
    def _transient(task, ws):
        raise RuntimeError("temporary glitch")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="flaky", assignee="worker")  # scratch ws
        res = kb.dispatch_once(conn, spawn_fn=_transient, failure_limit=3)
        assert tid not in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "ready"          # still retryable
        assert task.consecutive_failures == 1  # not force-blocked
    finally:
        conn.close()


def test_has_spawnable_ready_excludes_guarded_tasks(kanban_home, monkeypatch):
    """The stuck-probe must treat a respawn-guarded task (the dispatcher is
    deliberately holding it: auth backoff / recent success / open PR) as NOT
    stuck — while still reporting genuinely spawnable work as stuck."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    conn = kb.connect()
    try:
        kb.create_task(conn, title="ready-one", assignee="worker")
        # Fresh task, no guard reason → genuinely spawnable → IS a stuck
        # signal. This preserves the real "broken infra" alert.
        assert kb.has_spawnable_ready(conn) is True

        # Now simulate the dispatcher deliberately holding it this tick.
        monkeypatch.setattr(kb, "check_respawn_guard", lambda c, tid: "blocker_auth")
        assert kb.has_spawnable_ready(conn) is False
    finally:
        conn.close()


def test_malformed_task_clears_stuck_signal_after_one_tick(kanban_home, all_assignees_spawnable):
    """End-to-end: a malformed dir task looks 'stuck' before dispatch, but a
    single tick fast-blocks it, so the health probe no longer reports stuck —
    no more false 'check venv/PATH/credentials' warning."""
    conn = kb.connect()
    try:
        kb.create_task(
            conn, title="malformed", assignee="worker", workspace_kind="dir"
        )
        assert kb.has_spawnable_ready(conn) is True   # would warn "stuck"
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: 1)
        assert kb.has_spawnable_ready(conn) is False  # blocked → not stuck
    finally:
        conn.close()
