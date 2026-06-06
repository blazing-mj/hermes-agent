"""Tests for macOS launchd-domain gateway watchdog."""

import json
import subprocess
import time
import urllib.error

import hermes_cli.gateway as gateway_cli
from hermes_cli import gateway_watchdog


def _result(code: int = 0):
    return subprocess.CompletedProcess(["launchctl"], code, "", "")


def _print_result(state: str = "running", pid: int | None = 1234, code: int = 0):
    pid_line = f"\n\tpid = {pid}" if pid is not None else ""
    return subprocess.CompletedProcess(
        ["launchctl", "print"],
        code,
        f"gui/501/ai.hermes.gateway = {{\n\tstate = {state}{pid_line}\n}}\n",
        "",
    )


def _write_gateway_state(home, *, active_agents: int, age_seconds: float, state: str = "running"):
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds))
    path = home / "gateway_state.json"
    path.write_text(
        json.dumps({
            "gateway_state": state,
            "active_agents": active_agents,
            "updated_at": timestamp,
        }),
        encoding="utf-8",
    )
    return path


def test_watchdog_silent_when_gateway_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return _print_result(state="running", pid=1234)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert calls == [["launchctl", "print", f"gui/{gateway_watchdog.os.getuid()}/ai.hermes.gateway"]]


def test_watchdog_recovers_manual_bootout_without_alert_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / "ai.hermes.gateway.plist"
    plist.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    calls = []
    loaded = {"value": False}

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            if not loaded["value"]:
                return _result(113)
            return _print_result(state="running", pid=2468)
        if cmd[:2] == ["launchctl", "bootout"]:
            loaded["value"] = False
            return _result(0)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            loaded["value"] = True
            return _result(0)
        if cmd[:2] == ["launchctl", "kickstart"]:
            return _result(0)
        raise AssertionError(cmd)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    domain = f"gui/{gateway_watchdog.os.getuid()}"
    assert calls == [
        ["launchctl", "print", f"{domain}/ai.hermes.gateway"],
        ["launchctl", "bootout", f"{domain}/ai.hermes.gateway"],
        ["launchctl", "bootstrap", domain, str(plist)],
        ["launchctl", "kickstart", f"{domain}/ai.hermes.gateway"],
        ["launchctl", "print", f"{domain}/ai.hermes.gateway"],
    ]
    log = tmp_path / "logs" / gateway_watchdog.LOG_NAME
    assert "recovered default gateway" in log.read_text(encoding="utf-8")


def test_watchdog_recovers_loaded_but_not_running_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / "ai.hermes.gateway.plist"
    plist.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    calls = []
    healthy = {"value": False}

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            if healthy["value"]:
                return _print_result(state="running", pid=4321)
            return _print_result(state="not running", pid=None)
        if cmd[:2] == ["launchctl", "bootout"]:
            healthy["value"] = False
            return _result(0)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            healthy["value"] = True
            return _result(0)
        if cmd[:2] == ["launchctl", "kickstart"]:
            return _result(0)
        raise AssertionError(cmd)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert ["launchctl", "bootstrap", f"gui/{gateway_watchdog.os.getuid()}", str(plist)] in calls
    log = tmp_path / "logs" / gateway_watchdog.LOG_NAME
    assert "loaded but not live" in log.read_text(encoding="utf-8")


def test_watchdog_treats_unparseable_loaded_launchctl_output_as_healthy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(["launchctl", "print"], 0, "unexpected format\n", "")

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert calls == [["launchctl", "print", f"gui/{gateway_watchdog.os.getuid()}/ai.hermes.gateway"]]


def test_watchdog_retries_after_kickstart_before_declaring_recovery_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_watchdog.time, "sleep", lambda _seconds: None)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    prints = {"count": 0}

    def fake_run(cmd):
        cmd = list(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            prints["count"] += 1
            if prints["count"] < 3:
                return _print_result(state="not running", pid=None)
            return _print_result(state="running", pid=24680)
        return _result(0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert prints["count"] == 3


def test_watchdog_recovers_loaded_running_without_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    calls = []
    healthy = {"value": False}

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            if healthy["value"]:
                return _print_result(state="running", pid=9876)
            return _print_result(state="running", pid=None)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            healthy["value"] = True
        return _result(0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert any(cmd[:2] == ["launchctl", "bootstrap"] for cmd in calls)


def test_watchdog_retries_recovery_sequence_within_same_cycle(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_watchdog.time, "sleep", lambda _seconds: None)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    calls = []
    recovery_round = {"count": 0}

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            recovery_round["count"] += 1
            return _result(0)
        if cmd[:2] == ["launchctl", "print"]:
            if recovery_round["count"] >= 2:
                return _print_result(state="running", pid=2468)
            return _print_result(state="not running", pid=None)
        return _result(0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert len([cmd for cmd in calls if cmd[:2] == ["launchctl", "bootstrap"]]) == 2


def test_watchdog_returns_failure_when_all_recovery_rounds_leave_gateway_not_running(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_watchdog.time, "sleep", lambda _seconds: None)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    calls = []

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return _print_result(state="not running", pid=None)
        return _result(0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 2
    assert len([cmd for cmd in calls if cmd[:2] == ["launchctl", "bootstrap"]]) == gateway_watchdog.DEFAULT_RECOVERY_ROUNDS


def test_watchdog_returns_failure_when_recovery_cannot_load(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)

    def fake_run(cmd):
        return _result(113 if list(cmd)[1] == "print" else 0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 2


def test_watchdog_plist_polls_without_keepalive_busy_loop():
    plist = gateway_cli.generate_launchd_watchdog_plist()
    assert "<key>StartInterval</key>" in plist
    assert "<integer>120</integer>" in plist
    assert "<key>KeepAlive</key>" not in plist


def test_alert_failure_log_redacts_bot_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET_TOKEN_SHOULD_NOT_LOG")
    (tmp_path / "config.yaml").write_text("telegram:\n  allowed_chats:\n    - 170258889\n", encoding="utf-8")

    def fake_urlopen(url, data=None, timeout=None):
        raise urllib.error.URLError(url)

    monkeypatch.setattr(gateway_watchdog.urllib.request, "urlopen", fake_urlopen)
    assert gateway_watchdog._send_telegram_alert("test") is False
    log = (tmp_path / "logs" / gateway_watchdog.LOG_NAME).read_text(encoding="utf-8")
    assert "SECRET_TOKEN_SHOULD_NOT_LOG" not in log
    assert "bot" not in log


def test_watchdog_suppresses_recovery_for_recent_active_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_gateway_state(tmp_path, active_agents=1, age_seconds=30)
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return _print_result(state="running", pid=1234)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert calls == [["launchctl", "print", f"gui/{gateway_watchdog.os.getuid()}/ai.hermes.gateway"]]


def test_watchdog_recovers_stale_active_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_watchdog.time, "sleep", lambda _seconds: None)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("plist", encoding="utf-8")
    monkeypatch.setattr(gateway_watchdog, "_account_home", lambda: tmp_path)
    _write_gateway_state(tmp_path, active_agents=1, age_seconds=gateway_watchdog.DEFAULT_STUCK_BUSY_SECONDS + 5)

    recovering = {"value": False}
    calls = []

    def fake_run(cmd):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            if recovering["value"]:
                return _print_result(state="running", pid=5678)
            return _print_result(state="running", pid=1234)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            recovering["value"] = True
        return _result(0)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    domain = f"gui/{gateway_watchdog.os.getuid()}"
    assert ["launchctl", "bootout", f"{domain}/ai.hermes.gateway"] in calls
    assert ["launchctl", "bootstrap", domain, str(launch_agents / "ai.hermes.gateway.plist")] in calls
    log = tmp_path / "logs" / gateway_watchdog.LOG_NAME
    assert "stuck-busy" in log.read_text(encoding="utf-8")


def test_watchdog_does_not_recover_idle_gateway_with_stale_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_gateway_state(tmp_path, active_agents=0, age_seconds=gateway_watchdog.DEFAULT_STUCK_BUSY_SECONDS + 5)
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return _print_result(state="running", pid=1234)

    assert gateway_watchdog.check_once(alert=False, run=fake_run) == 0
    assert len(calls) == 1
