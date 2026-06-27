"""Stage C: the Worker connector (hermes_cli/team_os/worker_dispatch.py).

Stub runner (no real coding session). Covers: off-by-default, gate enforcement
(human_gate_required never auto-runs), happy dispatch, and fail-safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path("/Users/alfred/.hermes/hermes-agent")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.team_os.worker_dispatch import dispatch_worker  # noqa: E402

SAFE = {"source_ticket": "AGENTS-60", "human_gate_required": False, "files_to_touch": ["x.py"],
        "intended_behavior": "do x", "assertions": ["t"], "non_goals": ["n"],
        "commands": ["c"], "bounce_conditions": ["b"], "risk": "low"}
GATED = {**SAFE, "human_gate_required": True}


def test_gated_contract_never_auto_dispatches():
    called = []
    out = dispatch_worker(GATED, runner=lambda **k: called.append(1) or {}, enabled=True)
    assert out["dispatched"] is False and "human-gate" in out["reason"]
    assert called == []  # the worker engine was never called


def test_off_by_default_does_not_dispatch():
    called = []
    out = dispatch_worker(SAFE, runner=lambda **k: called.append(1) or {}, enabled=False)
    assert out["dispatched"] is False and "disabled" in out["reason"]
    assert called == []


def test_happy_dispatch_returns_handoff():
    def fake_runner(**kw):
        assert kw["contract"]["source_ticket"] == "AGENTS-60"
        assert kw["branch"] == "team-os/AGENTS-60"
        return {"worker_status": "completed", "changed_files": ["x.py"], "commit": "abc123"}
    out = dispatch_worker(SAFE, runner=fake_runner, enabled=True)
    assert out["dispatched"] is True
    assert out["worker_status"] == "completed"
    assert out["changed_files"] == ["x.py"]
    assert out["handoff"]["commit"] == "abc123"


def test_runner_error_is_fail_safe():
    def boom(**kw): raise RuntimeError("worktree blew up")
    out = dispatch_worker(SAFE, runner=boom, enabled=True)
    assert out["dispatched"] is False and "worktree blew up" in out["reason"]


def test_non_dict_handoff_handled():
    out = dispatch_worker(SAFE, runner=lambda **k: None, enabled=True)
    assert out["dispatched"] is False and "no handoff" in out["reason"]


def test_boundary_denied_surfaces_status():
    out = dispatch_worker(SAFE, enabled=True,
                          runner=lambda **k: {"worker_status": "boundary_denied", "boundary_violations": ["y.py"]})
    assert out["dispatched"] is True and out["worker_status"] == "boundary_denied"


def test_commit_worktree_produces_sha(tmp_path):
    """_commit_worktree commits uncommitted worker output and returns a sha."""
    import subprocess
    from hermes_cli.team_os.worker_dispatch import _commit_worktree
    wt = tmp_path / "wt"
    wt.mkdir()
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.co"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=wt, check=True)
    (wt / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=wt, check=True)
    (wt / "f.txt").write_text("a\nb\n")  # uncommitted worker change
    sha, err = _commit_worktree(wt, "AGENTS-77")
    assert err is None and sha and len(sha) == 40
    log = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "-1"], capture_output=True, text=True).stdout
    assert "AGENTS-77" in log


def test_commit_worktree_bad_path_fails_safe():
    from hermes_cli.team_os.worker_dispatch import _commit_worktree
    sha, err = _commit_worktree(Path("/nonexistent/worktree/xyz"), "AGENTS-1")
    assert sha is None and err  # graceful, no raise


# ── execute_spine: the wire that chains Worker → Validator (dormant by default) ──

def test_execute_spine_noop_when_worker_disabled(monkeypatch):
    import hermes_cli.team_os.worker_dispatch as wd
    monkeypatch.setattr(wd, "dispatch_worker", lambda c, **k: {"dispatched": False, "reason": "disabled"})
    out = wd.execute_spine({"source_ticket": "A-1"})
    assert out["ran"] is False and out["landable"] is False
    assert out["validator"] is None  # validator never reached


def test_execute_spine_landable_on_worker_commit_plus_validator_pass(monkeypatch):
    import hermes_cli.team_os.worker_dispatch as wd
    import hermes_cli.team_os.validator_dispatch as vd
    monkeypatch.setattr(wd, "dispatch_worker", lambda c, **k: {
        "dispatched": True, "worker_status": "completed", "commit": "a" * 40, "handoff": {"x": 1}})
    monkeypatch.setattr(vd, "dispatch_validator", lambda c, h, **k: {"verdict": "PASS", "bounce_count": 0})
    out = wd.execute_spine({"source_ticket": "A-1"})
    assert out["ran"] is True and out["landable"] is True and out["commit"] == "a" * 40


def test_execute_spine_not_landable_on_validator_bounce(monkeypatch):
    import hermes_cli.team_os.worker_dispatch as wd
    import hermes_cli.team_os.validator_dispatch as vd
    monkeypatch.setattr(wd, "dispatch_worker", lambda c, **k: {
        "dispatched": True, "worker_status": "completed", "commit": "b" * 40, "handoff": {}})
    monkeypatch.setattr(vd, "dispatch_validator", lambda c, h, **k: {"verdict": "BOUNCE"})
    out = wd.execute_spine({"source_ticket": "A-1"})
    assert out["ran"] is True and out["landable"] is False  # built but not validated


def test_execute_spine_worker_incomplete_skips_validator(monkeypatch):
    import hermes_cli.team_os.worker_dispatch as wd
    called = []
    monkeypatch.setattr(wd, "dispatch_worker", lambda c, **k: {
        "dispatched": True, "worker_status": "boundary_denied", "commit": None})
    import hermes_cli.team_os.validator_dispatch as vd
    monkeypatch.setattr(vd, "dispatch_validator", lambda c, h, **k: called.append(1) or {"verdict": "PASS"})
    out = wd.execute_spine({"source_ticket": "A-1"})
    assert out["landable"] is False and called == []  # validator not run on incomplete work
