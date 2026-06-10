"""Kill-switch for Team OS — Phase 9A.

Provides atomic, file-backed on/off state that gates loop-runner selection
and active dispatch.  Nothing in this module reaches the network or spawns
processes.

Usage::

    ks = KillSwitch(Path("~/.hermes/state/team-os-kill-switch.json"))
    if ks.is_enabled():
        raise KillSwitchActive("Team OS is halted")
    ks.enable(reason="operator halt")
    ks.disable()
    print(ks.status())
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class KillSwitchActive(RuntimeError):
    """Raised when an operation is blocked because the kill-switch is enabled."""


_DEFAULT_STATE = {"enabled": False}


@dataclass
class KillSwitch:
    """Atomic, file-backed kill-switch.

    Args:
        path: JSON file where state is persisted.  Created on first write.
            If the file does not exist the kill-switch is considered disabled.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """Return True if the kill-switch is currently enabled."""
        if _env_truthy(os.environ.get("HERMES_TEAM_OS_KILL")):
            return True
        return self._load().get("enabled", False) is True

    def status(self) -> dict[str, Any]:
        """Return the full kill-switch state dict.

        Returns at minimum ``{"enabled": bool}``.  When enabled, also includes
        ``"reason"`` and ``"enabled_at"`` (ISO-8601 UTC).
        """
        state = self._load()
        env_enabled = _env_truthy(os.environ.get("HERMES_TEAM_OS_KILL"))
        out: dict[str, Any] = {"enabled": env_enabled or state.get("enabled", False) is True}
        if env_enabled:
            out["reason"] = "HERMES_TEAM_OS_KILL environment override"
            out["source"] = "env"
            return out
        if out["enabled"]:
            out["reason"] = state.get("reason", "")
            out["enabled_at"] = state.get("enabled_at", "")
            if source := state.get("source"):
                out["source"] = source
            if read_error := state.get("read_error"):
                out["read_error"] = read_error
        return out

    # ------------------------------------------------------------------
    # Writes (atomic via tmp + rename)
    # ------------------------------------------------------------------

    def enable(self, *, reason: str = "") -> None:
        """Enable the kill-switch, persisting ``reason`` and timestamp."""
        state = {
            "enabled": True,
            "reason": reason,
            "enabled_at": _utc_iso(),
        }
        self._write(state)

    def disable(self) -> None:
        """Disable the kill-switch."""
        self._write({"enabled": False})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return dict(_DEFAULT_STATE)
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("kill-switch state must be a JSON object")
            return state
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            state = {
                "enabled": True,
                "reason": "kill-switch state file is unreadable or corrupt",
                "enabled_at": _utc_iso(),
                "source": "corrupt",
                "read_error": str(exc),
            }
            self._emit_corrupt_event(state)
            return state

    def _emit_corrupt_event(self, state: dict[str, Any]) -> None:
        """AGENTS-85: fail-closed on corrupt state must be visible, not silent.
        Best-effort append to the lifecycle event log; never raises."""
        try:
            log = Path("~/.hermes/logs/team-os-lifecycle-events.jsonl").expanduser()
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": _utc_iso(),
                    "event": "kill_switch_failed_closed_corrupt_state",
                    "path": str(self.path),
                    "read_error": state.get("read_error", ""),
                }) + "\n")
        except Exception:
            pass

    def _write(self, state: dict[str, Any]) -> None:
        """Atomically write ``state`` to disk (write-to-tmp + os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        # Write to a sibling tmp file then rename for atomicity.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".ks-tmp-",
            dir=str(self.path.parent),
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup of the tmp file before re-raising.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _utc_iso() -> str:
    """Return current UTC time as a compact ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}
