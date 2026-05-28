"""Phase 9A hardening: kill-switch fail-closed on corrupt/unreadable state file.

Scope (AGENTS-77):
    * Corrupt existing kill-switch JSON  => is_enabled() is True
                                         => status() exposes read_error + source.
    * Missing kill-switch file remains disabled (existing contract preserved).
    * HERMES_TEAM_OS_KILL env override still forces enabled (regression).
    * Invalid JSON variants: truncated, empty, wrong type at top level.
    * OSError / permission-denied is treated as corrupt (fail closed).

Strict TDD: these tests are written BEFORE the implementation change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Corrupt / unreadable state file → fail closed
# ---------------------------------------------------------------------------


def test_corrupt_json_is_enabled(tmp_path):
    """A file that exists but has bad JSON must be treated as enabled (fail closed)."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text("NOT VALID JSON {{{{")

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is True


def test_corrupt_json_status_exposes_read_error(tmp_path):
    """status() on a corrupt file must include 'read_error' and 'source' keys."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text("{bad json")

    ks = KillSwitch(state_file)
    s = ks.status()
    assert s["enabled"] is True
    assert "read_error" in s, "status() must expose read_error for corrupt file"
    assert "source" in s, "status() must expose source for corrupt file"
    assert s["source"] == "corrupt"


def test_truncated_json_is_enabled(tmp_path):
    """A truncated/partial JSON file must also fail closed."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text('{"enabled": fal')  # truncated

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is True


def test_empty_file_is_enabled(tmp_path):
    """An empty state file (exists but zero bytes) must fail closed."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text("")

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is True


def test_wrong_top_level_type_is_enabled(tmp_path):
    """Top-level JSON array or string (not a dict) must fail closed."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text("[]")  # valid JSON but wrong type

    ks = KillSwitch(state_file)
    # Valid JSON with the wrong top-level type is still unusable state.
    assert ks.is_enabled() is True


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions only")
def test_permission_denied_is_enabled(tmp_path):
    """An unreadable (chmod 000) state file must fail closed."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text('{"enabled": true}')
    state_file.chmod(0o000)

    try:
        ks = KillSwitch(state_file)
        # Running as root bypasses permission checks; skip assertion in that case.
        if os.getuid() != 0:
            assert ks.is_enabled() is True
    finally:
        # Restore so tmp_path cleanup can delete the file.
        state_file.chmod(0o644)


# ---------------------------------------------------------------------------
# Missing file → still disabled (existing contract must not regress)
# ---------------------------------------------------------------------------


def test_missing_file_is_disabled(tmp_path):
    """No state file at all → kill-switch is off (fail-open for new installs)."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "nonexistent.json")
    assert ks.is_enabled() is False


def test_missing_file_status_is_disabled(tmp_path):
    """status() with no state file must return enabled=False."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    ks = KillSwitch(tmp_path / "nonexistent.json")
    s = ks.status()
    assert s["enabled"] is False
    assert "read_error" not in s


# ---------------------------------------------------------------------------
# Env override still wins even over corrupt file
# ---------------------------------------------------------------------------


def test_env_override_enabled_with_corrupt_file(tmp_path, monkeypatch):
    """HERMES_TEAM_OS_KILL=1 must force enabled regardless of corrupt file."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    monkeypatch.setenv("HERMES_TEAM_OS_KILL", "1")
    state_file = tmp_path / "ks.json"
    state_file.write_text("GARBAGE")

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is True
    s = ks.status()
    assert s["source"] == "env"


def test_env_override_enabled_with_missing_file(tmp_path, monkeypatch):
    """HERMES_TEAM_OS_KILL=1 must force enabled even when file is missing."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    monkeypatch.setenv("HERMES_TEAM_OS_KILL", "1")
    ks = KillSwitch(tmp_path / "nonexistent.json")
    assert ks.is_enabled() is True


# ---------------------------------------------------------------------------
# Valid disabled state still works
# ---------------------------------------------------------------------------


def test_valid_disabled_json_is_disabled(tmp_path):
    """A well-formed disabled state file must remain disabled."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text(json.dumps({"enabled": False}))

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is False


def test_valid_enabled_json_is_enabled(tmp_path):
    """A well-formed enabled state file must still be enabled."""
    from hermes_cli.team_os.kill_switch import KillSwitch

    state_file = tmp_path / "ks.json"
    state_file.write_text(json.dumps({"enabled": True, "reason": "test"}))

    ks = KillSwitch(state_file)
    assert ks.is_enabled() is True
