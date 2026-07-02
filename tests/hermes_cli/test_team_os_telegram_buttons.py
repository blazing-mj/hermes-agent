"""AGENTS-243: Telegram inline Approve/Reject/Question for Needs-MJ pings.

Covers the two integration seams without a live bot:
  - send_message_tool threads an inline_keyboard into the Telegram send
  - the intake motor attaches lm:<action>:<ticket> buttons to the ping
The gateway callback path is exercised by hand-built fakes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_build_inline_keyboard_shapes_rows():
    pytest.importorskip("telegram")
    from tools.send_message_tool import _build_inline_keyboard

    kb = _build_inline_keyboard([
        [("✅ Approve", "lm:approve:AGENTS-236"), ("❌ Reject", "lm:reject:AGENTS-236")],
        [("💬 Question", "lm:question:AGENTS-236")],
    ])
    assert kb is not None
    rows = kb.inline_keyboard
    assert len(rows) == 2 and len(rows[0]) == 2 and len(rows[1]) == 1
    assert rows[0][0].callback_data == "lm:approve:AGENTS-236"
    assert rows[1][0].callback_data == "lm:question:AGENTS-236"


def test_build_inline_keyboard_empty_is_none():
    from tools.send_message_tool import _build_inline_keyboard
    assert _build_inline_keyboard(None) is None
    assert _build_inline_keyboard([]) is None


def test_callback_data_under_64_bytes():
    # Telegram hard-caps callback_data at 64 bytes; our longest realistic ticket.
    for action in ("approve", "reject", "question"):
        assert len(f"lm:{action}:AGENTS-100000".encode()) <= 64


def test_motor_ping_attaches_buttons(monkeypatch):
    """_send_needs_mj_ping_once must pass an lm:* inline keyboard for the ticket."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "intake_motor", ROOT / "scripts" / "team_os_linear_intake_motor.py")
    motor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(motor)

    captured = {}

    def fake_send(args):
        captured.update(args)
        return json.dumps({"success": True, "message_id": 1})

    monkeypatch.setattr(motor, "_load_env_file", lambda: None, raising=False)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool",
                        type("M", (), {"send_message_tool": staticmethod(fake_send)}))

    class FakeState:
        def get_outbox_event(self, _id): return {"payload": {}}
    monkeypatch.setattr(motor, "_assign_to_viewer", lambda t: "viewer", raising=False)
    monkeypatch.setattr(motor, "_set_payload_marker", lambda *a, **k: None, raising=False)

    res = motor._send_needs_mj_ping_once(
        FakeState(), "AGENTS-236",
        {"title": "Gated cleanup", "description": "do the thing", "url": "http://x"},
        outbox_id=7, chain={})
    assert res["sent"] is True
    kb = captured.get("inline_keyboard")
    assert kb, "ping must carry an inline keyboard"
    flat = [cb for row in kb for (_t, cb) in row]
    assert "lm:approve:AGENTS-236" in flat
    assert "lm:reject:AGENTS-236" in flat
    assert "lm:question:AGENTS-236" in flat


def test_approve_builds_human_override_move():
    """The Approve path must move Needs-MJ->Approved as 'mj' (human override),
    which the restricted writer accepts without conditions."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rlw", ROOT / "scripts" / "restricted_linear_writer.py")
    rlw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rlw)
    # exactly the proposal the gateway callback constructs
    validated = rlw._validate_status_action({
        "action": "status", "issue": "AGENTS-236",
        "from": "Needs-MJ", "to": "Approved", "by": "mj",
    })
    assert validated["to"] == "Approved" and validated["by"] == "mj"

    rejected = rlw._validate_status_action({
        "action": "status", "issue": "AGENTS-236",
        "from": "Needs-MJ", "to": "Rejected", "by": "mj",
    })
    assert rejected["to"] == "Rejected"


def test_rejected_lane_in_allowlist():
    spec = json.loads((ROOT / "docs/team-os/board-transitions.json").read_text())
    assert "Rejected" in spec["lanes"]
    assert "Approved" in spec["lanes"]
