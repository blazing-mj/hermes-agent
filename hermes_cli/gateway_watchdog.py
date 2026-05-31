"""macOS launchd-domain watchdog for the Hermes gateway.

This intentionally runs outside the gateway process.  Its job is not to
observe crashes (launchd KeepAlive handles those), but to recover the rarer
failure where the launchd job is removed from the gui/<uid> domain entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_LABEL = "ai.hermes.gateway"
DEFAULT_PLIST = "ai.hermes.gateway.plist"
LOG_NAME = "gateway-domain-watchdog.log"
ALERT_COOLDOWN_SECONDS = 30 * 60


def _account_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or _account_home() / ".hermes").expanduser()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _plist_path() -> Path:
    return _account_home() / "Library" / "LaunchAgents" / DEFAULT_PLIST


def _log(message: str) -> None:
    home = _hermes_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with (log_dir / LOG_NAME).open("a", encoding="utf-8") as f:
        f.write(f"{ts} {message}\n")


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _telegram_chat_id(home: Path, env: dict[str, str]) -> str | None:
    for key in ("TELEGRAM_ALERT_CHAT_ID", "TELEGRAM_ALLOWED_CHATS", "TELEGRAM_CHAT_ID"):
        raw = env.get(key) or os.environ.get(key)
        if raw:
            return raw.split(",", 1)[0].strip()

    cfg = home / "config.yaml"
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        allowed = (data.get("telegram") or {}).get("allowed_chats")
        if isinstance(allowed, (list, tuple)) and allowed:
            return str(allowed[0])
        if isinstance(allowed, str) and allowed.strip():
            return allowed.split(",", 1)[0].strip()
    except Exception:
        return None
    return None


def _alert_state_path(home: Path) -> Path:
    return home / "state" / "gateway-domain-watchdog-alert.json"


def _alert_allowed(home: Path, now: float) -> bool:
    path = _alert_state_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = float(data.get("last_alert_at") or 0)
    except Exception:
        last = 0.0
    if now - last < ALERT_COOLDOWN_SECONDS:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_alert_at": now}), encoding="utf-8")
    except OSError:
        pass
    return True


def _send_telegram_alert(message: str) -> bool:
    home = _hermes_home()
    env = {**_load_env(home / ".env"), **_load_env(Path.home() / ".hermes" / ".env")}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = _telegram_chat_id(home, env)
    if not token or not chat_id:
        _log("alert skipped: missing Telegram token or chat id")
        return False
    if not _alert_allowed(home, time.time()):
        _log("alert suppressed: cooldown active")
        return False

    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=body, timeout=10) as resp:  # noqa: S310 - configured Telegram API endpoint
            ok = 200 <= getattr(resp, "status", 0) < 300
            _log(f"alert sent: {ok}")
            return ok
    except Exception as exc:
        # Never log urllib exception text: it can include the bot-token URL.
        _log(f"alert failed: {type(exc).__name__}")
        return False


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def is_gateway_loaded(run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run) -> bool:
    result = run(["launchctl", "print", f"{_domain()}/{DEFAULT_LABEL}"])
    return result.returncode == 0


def recover_gateway(run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run) -> bool:
    plist = _plist_path()
    if not plist.exists():
        _log(f"recover failed: missing plist {plist}")
        return False

    domain = _domain()
    run(["launchctl", "bootout", f"{domain}/{DEFAULT_LABEL}"])
    run(["launchctl", "bootstrap", domain, str(plist)])
    run(["launchctl", "kickstart", f"{domain}/{DEFAULT_LABEL}"])
    return is_gateway_loaded(run)


def check_once(*, alert: bool = True, run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run) -> int:
    """Return 0 when healthy/recovered, 2 when recovery failed."""
    if is_gateway_loaded(run):
        return 0

    _log(f"domain missing: {_domain()}/{DEFAULT_LABEL}; attempting bootstrap")
    recovered = recover_gateway(run)
    if recovered:
        msg = "⚠️ Hermes default gateway was missing from launchd; watchdog re-bootstrapped it."
        _log("recovered default gateway launchd domain membership")
        if alert:
            _send_telegram_alert(msg)
        return 0

    msg = "🔴 Hermes default gateway launchd watchdog failed to re-bootstrap ai.hermes.gateway."
    _log("recovery failed")
    if alert:
        _send_telegram_alert(msg)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    alert = "--no-alert" not in argv
    return check_once(alert=alert)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
