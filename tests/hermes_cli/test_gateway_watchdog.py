"""Tests for macOS launchd-domain gateway watchdog."""

import subprocess
import urllib.error

import hermes_cli.gateway as gateway_cli
from hermes_cli import gateway_watchdog


def _result(code: int = 0):
    return subprocess.CompletedProcess(["launchctl"], code, "", "")


def test_watchdog_silent_when_gateway_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return _result(0)

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
            return _result(0 if loaded["value"] else 113)
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
