"""Phase 9A: kill-switch + sandbox stability gates tests.

Strict TDD: these specs are written before the implementation.

Boundaries enforced:
    * Kill-switch state persists atomically and can be read/set/cleared via CLI.
    * Loop runner (select_next_task) returns no eligible task when kill-switch is enabled.
    * Active dispatch (run_active_dispatch) raises/fails immediately when kill-switch is enabled.
    * Telegram authorized stop command halts Team OS with a plain response.
    * Telegram stop command is rejected (plain response) when user is not authorized.
    * Failure / timeout / reclaim paths in active dispatch fail closed (blocks_task=True).
    * No network or production paths are introduced.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from argparse import Namespace
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Kill-switch state
# ---------------------------------------------------------------------------


def test_kill_switch_enabled_by_default_is_false(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    assert ks.is_enabled() is False


def test_kill_switch_enable_persists(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test")
    assert ks.is_enabled() is True

    # Re-read from disk — persists across instances
    ks2 = KillSwitch(tmp_path / "ks.json")
    assert ks2.is_enabled() is True


def test_kill_switch_disable(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="test")
    ks.disable()
    assert ks.is_enabled() is False


def test_kill_switch_status_dict(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="operator halt")

    status = ks.status()
    assert status["enabled"] is True
    assert "reason" in status
    assert status["reason"] == "operator halt"
    assert "enabled_at" in status


def test_kill_switch_status_when_disabled(tmp_path):
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    status = ks.status()
    assert status["enabled"] is False


def test_kill_switch_corrupt_state_file_fails_closed(tmp_path):
    """A present-but-unparsable state file must halt Team OS."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    path = tmp_path / "ks.json"
    path.write_text("{not valid json", encoding="utf-8")

    ks = KillSwitch(path)
    assert ks.is_enabled() is True
    status = ks.status()
    assert status["enabled"] is True
    assert "corrupt" in status["reason"] or "unreadable" in status["reason"]
    assert status["source"] == "corrupt"


def test_kill_switch_unreadable_state_file_fails_closed(tmp_path, monkeypatch):
    """A read error on an existing state file must halt Team OS."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    path = tmp_path / "ks.json"
    path.write_text('{"enabled": false}', encoding="utf-8")
    original_read_text = Path.read_text

    def fail_for_state_file(self, *args, **kwargs):
        if self == path:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_state_file)

    ks = KillSwitch(path)
    assert ks.is_enabled() is True
    status = ks.status()
    assert status["enabled"] is True
    assert status["source"] == "corrupt"


def test_kill_switch_missing_file_remains_disabled(tmp_path):
    """Absent state file is normal startup state, not a read failure."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "missing.json")
    assert ks.is_enabled() is False
    assert ks.status()["enabled"] is False


def test_kill_switch_env_override_still_forces_enabled(tmp_path, monkeypatch):
    """Environment override keeps precedence over clean disabled state."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    monkeypatch.setenv("HERMES_TEAM_OS_KILL", "1")
    ks = KillSwitch(tmp_path / "ks.json")
    ks.disable()

    assert ks.is_enabled() is True
    status = ks.status()
    assert status["enabled"] is True
    assert status["source"] == "env"


def test_kill_switch_disable_recovers_from_corrupt_state_file(tmp_path):
    """Operator recovery: disable() overwrites a corrupt fail-closed state."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    path = tmp_path / "ks.json"
    path.write_text("{corrupt", encoding="utf-8")
    ks = KillSwitch(path)
    assert ks.is_enabled() is True

    ks.disable()
    assert ks.is_enabled() is False


def test_kill_switch_atomic_write(tmp_path):
    """Enable/disable should not leave partial JSON on disk."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_path = tmp_path / "ks.json"
    ks = KillSwitch(state_path)
    ks.enable(reason="atomic")
    # File must be valid JSON after the write
    raw = json.loads(state_path.read_text())
    assert raw["enabled"] is True
    ks.disable()
    raw2 = json.loads(state_path.read_text())
    assert raw2["enabled"] is False


# ---------------------------------------------------------------------------
# Kill-switch gating in select_next_task
# ---------------------------------------------------------------------------


def test_select_next_task_blocked_by_kill_switch(tmp_path):
    """When kill-switch is enabled, select_next_task returns no eligible task."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="kill-switch test")

    tasks = [
        LoopTask(
            task_id="T1",
            title="some task",
            priority=5,
            status="ready",
            shifts=("day",),
            approval_status=None,
            quota_confidence="high",
            task_confidence="high",
        )
    ]
    decision = select_next_task(tasks, current_shift="day", kill_switch=ks)
    assert decision.selected_task_id is None
    assert "T1" in decision.skipped_task_ids
    assert "kill-switch" in decision.skip_reasons["T1"]


def test_select_next_task_passes_when_kill_switch_disabled(tmp_path):
    """When kill-switch is off, select_next_task works as before."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    ks = KillSwitch(tmp_path / "ks.json")
    # leave disabled

    tasks = [
        LoopTask(
            task_id="T1",
            title="some task",
            priority=5,
            status="ready",
            shifts=("day",),
            approval_status=None,
            quota_confidence="high",
            task_confidence="high",
        )
    ]
    decision = select_next_task(tasks, current_shift="day", kill_switch=ks)
    assert decision.selected_task_id == "T1"


def test_select_next_task_kill_switch_none_is_backward_compat():
    """When kill_switch=None (default), behavior is unchanged from Phase 8."""
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="T1",
            title="good task",
            priority=1,
            status="ready",
            shifts=("day",),
            approval_status=None,
            quota_confidence="high",
        )
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id == "T1"


def test_select_next_task_blocks_unrecorded_approval_when_required():
    """Strict approval mode must fail closed on approval_status=None."""
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    task = LoopTask(
        task_id="T-unapproved",
        title="needs explicit approval",
        priority=10,
        status="ready",
        shifts=("day",),
        approval_status=None,
        quota_confidence="high",
        task_confidence="high",
    )

    decision = select_next_task([task], current_shift="day", require_approval=True)

    assert decision.selected_task_id is None
    assert decision.skip_reasons["T-unapproved"] == "approval not recorded"


def test_select_next_task_allows_unrecorded_approval_by_default():
    """Phase 9A strict approval is opt-in so Phase 8 behavior stays compatible."""
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    task = LoopTask(
        task_id="T-default",
        title="legacy eligible",
        priority=10,
        status="ready",
        shifts=("day",),
        approval_status=None,
        quota_confidence="high",
        task_confidence="high",
    )

    decision = select_next_task([task], current_shift="day")

    assert decision.selected_task_id == "T-default"


# ---------------------------------------------------------------------------
# Kill-switch gating in active dispatch
# ---------------------------------------------------------------------------


def test_run_active_dispatch_blocked_by_kill_switch(tmp_path):
    """Active dispatch must fail closed when kill-switch is enabled."""
    from hermes_cli.team_os.kill_switch import KillSwitch, KillSwitchActive
    from hermes_cli.team_os.loop_runner import (
        LoopTask,
        SandboxWorkspace,
        run_active_dispatch,
    )

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="halt for test")

    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)

    task = LoopTask(
        task_id="T1",
        title="test",
        priority=1,
        status="ready",
        shifts=("day",),
    )

    with pytest.raises(KillSwitchActive):
        run_active_dispatch(
            task,
            workspace=ws,
            worker_command=["true"],
            heartbeat_path=tmp_path / "hb",
            lock_path=tmp_path / "lock",
            owner="test",
            max_runtime_seconds=5.0,
            heartbeat_stale_seconds=5.0,
            kill_switch=ks,
        )

    assert not (tmp_path / "lock").exists()


_HEARTBEAT_FOREVER_WORKER = textwrap.dedent(
    """
    import os
    import pathlib
    import time

    heartbeat = pathlib.Path(os.environ["HERMES_HEARTBEAT_PATH"])
    while True:
        heartbeat.write_text(str(time.time()))
        time.sleep(0.05)
    """
)


def test_run_active_dispatch_aborts_when_kill_switch_enabled_mid_run(tmp_path):
    """A live worker must stop shortly after the kill-switch is enabled."""
    from hermes_cli.team_os.kill_switch import KillSwitch
    from hermes_cli.team_os.loop_runner import LoopTask, SandboxWorkspace, run_active_dispatch

    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)
    worker_script = tmp_path / "heartbeat_forever.py"
    worker_script.write_text(_HEARTBEAT_FOREVER_WORKER)
    ks = KillSwitch(tmp_path / "ks.json")
    heartbeat_path = tmp_path / "hb_midrun"

    def enable_after_first_heartbeat() -> None:
        if heartbeat_path.exists() and not ks.is_enabled():
            ks.enable(reason="mid-run abort test")

    result = run_active_dispatch(
        LoopTask(task_id="T-abort", title="abort", priority=1, status="ready", shifts=("day",)),
        workspace=ws,
        worker_command=[sys.executable, str(worker_script)],
        heartbeat_path=heartbeat_path,
        lock_path=tmp_path / "lock_abort",
        owner="test-abort",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.05,
        kill_switch=ks,
        poll_hook=enable_after_first_heartbeat,
    )

    assert result.status == "aborted"
    assert result.blocks_task is True
    assert "kill-switch" in result.reason
    assert not (tmp_path / "lock_abort").exists()


# ---------------------------------------------------------------------------
# Active dispatch stability: fail closed paths
# ---------------------------------------------------------------------------


_FAIL_WORKER = textwrap.dedent(
    """
    import sys
    sys.exit(1)
    """
)

_SLOW_WORKER = textwrap.dedent(
    """
    import time
    time.sleep(60)
    """
)


def test_dispatch_timeout_blocks_task(tmp_path):
    """Timeout path must produce blocks_task=True."""
    from hermes_cli.team_os.loop_runner import (
        LoopTask,
        SandboxWorkspace,
        run_active_dispatch,
    )

    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)

    worker_script = tmp_path / "slow.py"
    worker_script.write_text(_SLOW_WORKER)

    task = LoopTask(task_id="T-timeout", title="slow", priority=1, status="ready", shifts=("day",))
    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, str(worker_script)],
        heartbeat_path=tmp_path / "hb_timeout",
        lock_path=tmp_path / "lock_timeout",
        owner="test-timeout",
        max_runtime_seconds=0.3,
        heartbeat_stale_seconds=1.0,
        poll_interval=0.05,
    )
    assert result.status == "timeout"
    assert result.blocks_task is True


def test_dispatch_failure_blocks_task(tmp_path):
    """Non-zero exit blocks task."""
    from hermes_cli.team_os.loop_runner import (
        LoopTask,
        SandboxWorkspace,
        run_active_dispatch,
    )

    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)

    worker_script = tmp_path / "fail.py"
    worker_script.write_text(_FAIL_WORKER)

    task = LoopTask(task_id="T-fail", title="fail", priority=1, status="ready", shifts=("day",))
    result = run_active_dispatch(
        task,
        workspace=ws,
        worker_command=[sys.executable, str(worker_script)],
        heartbeat_path=tmp_path / "hb_fail",
        lock_path=tmp_path / "lock_fail",
        owner="test-fail",
        max_runtime_seconds=10.0,
        heartbeat_stale_seconds=5.0,
        poll_interval=0.05,
    )
    assert result.status == "failed"
    assert result.blocks_task is True


def test_stale_lock_from_dead_pid_is_reclaimable(tmp_path):
    """A lock left by a dead process must not permanently stall the runner."""
    from hermes_cli.team_os.loop_runner import acquire_runner_lock

    lock_path = tmp_path / "runner.lock"
    lock_path.write_text(json.dumps({"owner": "dead", "pid": 99999999, "ts": time.time()}))

    lock = acquire_runner_lock(lock_path, owner="new", reclaim=True, stale_after_seconds=60.0)

    payload = json.loads(lock_path.read_text())
    assert payload["owner"] == "new"
    assert payload["pid"] == os.getpid()
    lock.release()
    assert not lock_path.exists()


def test_active_dispatch_reclaims_dead_pid_lock(tmp_path):
    """Active dispatch must wire dead-lock reclaim, not just expose the helper."""
    from hermes_cli.team_os.loop_runner import LoopTask, SandboxWorkspace, run_active_dispatch

    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)
    lock_path = tmp_path / "active.lock"
    lock_path.write_text(json.dumps({"owner": "dead", "pid": 99999999, "ts": time.time()}))

    result = run_active_dispatch(
        LoopTask(task_id="T-reclaim", title="reclaim", priority=1, status="ready", shifts=("day",)),
        workspace=ws,
        worker_command=[sys.executable, "-c", "pass"],
        heartbeat_path=tmp_path / "hb_reclaim",
        lock_path=lock_path,
        owner="new-active",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
    )

    assert result.status == "succeeded"
    assert not lock_path.exists()


def test_live_lock_is_not_reclaimed(tmp_path):
    """A fresh lock owned by a live PID still blocks a second runner."""
    from hermes_cli.team_os.loop_runner import RunnerAlreadyActive, acquire_runner_lock

    lock_path = tmp_path / "runner.lock"
    lock_path.write_text(json.dumps({"owner": "live", "pid": os.getpid(), "ts": time.time()}))

    with pytest.raises(RunnerAlreadyActive):
        acquire_runner_lock(lock_path, owner="new", reclaim=True, stale_after_seconds=60.0)


def test_sandbox_worker_env_excludes_pythonpath(tmp_path, monkeypatch):
    """Sandbox env must not inherit PYTHONPATH from the parent process."""
    from hermes_cli.team_os.loop_runner import LoopTask, SandboxWorkspace, run_active_dispatch

    monkeypatch.setenv("PYTHONPATH", "/dangerous/repo/path")
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    ws = SandboxWorkspace.create(sandbox, allowed_prefix=tmp_path)
    worker_script = tmp_path / "env_probe.py"
    out_path = sandbox / "pythonpath.txt"
    worker_script.write_text(
        "import os, pathlib\n"
        f"pathlib.Path({str(out_path)!r}).write_text(os.environ.get('PYTHONPATH', '<unset>'))\n"
    )

    result = run_active_dispatch(
        LoopTask(task_id="T-env", title="env", priority=1, status="ready", shifts=("day",)),
        workspace=ws,
        worker_command=[sys.executable, str(worker_script)],
        heartbeat_path=tmp_path / "hb_env",
        lock_path=tmp_path / "lock_env",
        owner="test-env",
        max_runtime_seconds=5.0,
        heartbeat_stale_seconds=5.0,
    )

    assert result.status == "succeeded"
    assert out_path.read_text() == "<unset>"


# ---------------------------------------------------------------------------
# CLI kill-switch subcommand
# ---------------------------------------------------------------------------


def test_cli_kill_switch_enable(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    args = Namespace(
        team_os_command="kill-switch",
        ks_action="enable",
        reason="operator halt",
        state_file=str(tmp_path / "ks.json"),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0

    # Verify persisted
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    assert ks.is_enabled() is True


def test_cli_kill_switch_disable(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    # Pre-enable
    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="pre")

    args = Namespace(
        team_os_command="kill-switch",
        ks_action="disable",
        reason=None,
        state_file=str(tmp_path / "ks.json"),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    assert KillSwitch(tmp_path / "ks.json").is_enabled() is False


def test_cli_kill_switch_status_json(tmp_path, capsys):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="status test")

    args = Namespace(
        team_os_command="kill-switch",
        ks_action="status",
        reason=None,
        state_file=str(tmp_path / "ks.json"),
        output=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["enabled"] is True
    assert data["reason"] == "status test"


def test_cli_kill_switch_status_output_path(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "ks.json")
    ks.enable(reason="file output")

    out_path = tmp_path / "status_out.json"
    args = Namespace(
        team_os_command="kill-switch",
        ks_action="status",
        reason=None,
        state_file=str(tmp_path / "ks.json"),
        output=str(out_path),
    )
    rc = cmd_team_os(args)
    assert rc == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["enabled"] is True


# ---------------------------------------------------------------------------
# Telegram authorized stop command
# ---------------------------------------------------------------------------


def _telegram_mocks():
    """Mock the telegram module ONLY when it isn't actually installed.

    This runs at import time and uses sys.modules.setdefault, so when the real
    telegram lib was not yet imported it permanently installed a MagicMock over
    the key for the whole session — leaking into other tests that import the
    real lib (e.g. test_team_os_telegram_buttons, which then saw a mock
    InlineKeyboardMarkup and failed). If the real lib is present, use it and
    never mock; only stub when genuinely headless.
    """
    from unittest.mock import MagicMock

    try:
        import telegram  # noqa: F401 - real lib present → use it, never leak a mock
        return
    except ImportError:
        pass

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_telegram_mocks()


from gateway.platforms.telegram import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


def _make_adapter():
    from unittest.mock import AsyncMock, MagicMock

    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestTelegramStopTeamOS:
    @pytest.mark.asyncio
    async def test_authorized_stop_command_enables_kill_switch(self, tmp_path):
        """Authorized /stop_team_os command enables kill-switch and replies plainly."""
        from unittest.mock import AsyncMock, MagicMock

        adapter = _make_adapter()

        msg = MagicMock()
        msg.text = "/stop_team_os"
        msg.from_user.id = 999
        msg.from_user.username = "mj"
        msg.chat.id = 999
        msg.chat.type = "private"
        msg.message_id = 1

        adapter._bot.send_message = AsyncMock()

        ks_path = tmp_path / "ks.json"
        result = await adapter.handle_team_os_stop(
            message=msg,
            authorized_user_ids=["999"],
            kill_switch_path=str(ks_path),
        )

        assert result is True  # command handled
        adapter._bot.send_message.assert_called_once()
        call_kwargs = adapter._bot.send_message.call_args
        # Plain response — no markdown parse_mode
        text_sent = call_kwargs[1].get("text", "") or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Team OS" in text_sent or "stop" in text_sent.lower() or "halted" in text_sent.lower()
        # Kill-switch must be enabled on disk
        from hermes_cli.team_os.kill_switch import KillSwitch

        assert KillSwitch(ks_path).is_enabled() is True

    @pytest.mark.asyncio
    async def test_unauthorized_stop_command_rejected(self, tmp_path):
        """Unauthorized /stop_team_os is rejected and kill-switch is not touched."""
        from unittest.mock import AsyncMock, MagicMock

        adapter = _make_adapter()

        msg = MagicMock()
        msg.text = "/stop_team_os"
        msg.from_user.id = 666
        msg.from_user.username = "stranger"
        msg.chat.id = 666
        msg.chat.type = "private"
        msg.message_id = 2

        adapter._bot.send_message = AsyncMock()

        ks_path = tmp_path / "ks.json"
        result = await adapter.handle_team_os_stop(
            message=msg,
            authorized_user_ids=["999"],  # 666 not in list
            kill_switch_path=str(ks_path),
        )

        assert result is False  # rejected
        # Still sends a plain rejection notice
        adapter._bot.send_message.assert_called_once()
        text_sent = adapter._bot.send_message.call_args[1].get("text", "") or ""
        assert "not authorized" in text_sent.lower() or "unauthorized" in text_sent.lower()
        # Kill-switch must NOT be enabled
        from hermes_cli.team_os.kill_switch import KillSwitch

        assert KillSwitch(ks_path).is_enabled() is False

    @pytest.mark.asyncio
    async def test_stop_command_response_is_plain_text(self, tmp_path):
        """Response must be plain text — no parse_mode=Markdown on the bot call."""
        from unittest.mock import AsyncMock, MagicMock

        adapter = _make_adapter()

        msg = MagicMock()
        msg.text = "/stop_team_os"
        msg.from_user.id = 100
        msg.from_user.username = "op"
        msg.chat.id = 100
        msg.chat.type = "private"
        msg.message_id = 3

        adapter._bot.send_message = AsyncMock()

        ks_path = tmp_path / "ks.json"
        await adapter.handle_team_os_stop(
            message=msg,
            authorized_user_ids=["100"],
            kill_switch_path=str(ks_path),
        )

        call_kwargs = adapter._bot.send_message.call_args[1]
        # parse_mode must be absent or None for plain text
        assert call_kwargs.get("parse_mode") is None


    @pytest.mark.asyncio
    async def test_stop_command_empty_authorization_fails_closed(self, tmp_path, monkeypatch):
        """No explicit allowed user list means the stop command is rejected."""
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock()
        msg = MagicMock()
        msg.text = "/stop_team_os"
        msg.from_user.id = 100
        msg.from_user.username = "op"
        msg.chat.id = 100
        msg.chat.type = "private"

        ks_path = tmp_path / "ks.json"
        result = await adapter.handle_team_os_stop(
            message=msg,
            authorized_user_ids=[],
            kill_switch_path=str(ks_path),
        )

        assert result is False
        from hermes_cli.team_os.kill_switch import KillSwitch

        assert KillSwitch(ks_path).is_enabled() is False

    @pytest.mark.asyncio
    async def test_handle_command_routes_teamos_stop_alias(self, tmp_path, monkeypatch):
        """The actual Telegram command handler must route /teamos_stop to the kill-switch."""
        from unittest.mock import AsyncMock, MagicMock
        from hermes_cli.team_os.kill_switch import KillSwitch

        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "100")
        monkeypatch.setenv("HERMES_TEAM_OS_KILL_SWITCH_PATH", str(tmp_path / "ks.json"))
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock()
        adapter.handle_message = AsyncMock()
        update = MagicMock()
        update.update_id = 123
        update.message.text = "/teamos_stop"
        update.message.from_user.id = 100
        update.message.from_user.username = "op"
        update.message.chat.id = 100
        update.message.chat.type = "private"

        await adapter._handle_command(update, MagicMock())

        assert KillSwitch(tmp_path / "ks.json").is_enabled() is True
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_command_routes_stop_team_os_bot_suffix(self, tmp_path, monkeypatch):
        """The actual Telegram command handler must strip @bot suffixes."""
        from unittest.mock import AsyncMock, MagicMock
        from hermes_cli.team_os.kill_switch import KillSwitch

        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "100")
        monkeypatch.setenv("HERMES_TEAM_OS_KILL_SWITCH_PATH", str(tmp_path / "ks.json"))
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock()
        adapter.handle_message = AsyncMock()
        update = MagicMock()
        update.update_id = 124
        update.message.text = "/stop_team_os@HermesBot"
        update.message.from_user.id = 100
        update.message.from_user.username = "op"
        update.message.chat.id = 100
        update.message.chat.type = "private"

        await adapter._handle_command(update, MagicMock())

        assert KillSwitch(tmp_path / "ks.json").is_enabled() is True
        adapter.handle_message.assert_not_called()
