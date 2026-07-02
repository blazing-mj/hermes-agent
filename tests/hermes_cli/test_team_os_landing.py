"""P0 (completion-plan brief): the landing driveshaft.

Covers the three pieces that make a validated Worker commit actually reach the
codebase (locally — no remote pushes, MJ pushes manually):
  1. record_landing_evidence — posts the sha onto the spine chain, the exact
     surface _landing_evidence reads (round-tripped here with a REAL git repo).
  2. _land_commit_locally — ff-only update of the repo's LOCAL main ref; loud
     refusal on non-ff; idempotent when already landed.
  3. run_integrator_auto_land — flag OFF ⇒ byte-for-byte state-only landing;
     flag ON ⇒ lands the evidenced commit BEFORE any state change, and a
     failed landing bounces loudly with no ceremony Done.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_db  # noqa: E402
from hermes_cli.team_os import linear_webhook as lw  # noqa: E402

TICKET = "AGENTS-777"
PROJECT = "Hermes System"
BOARD = "hermes-system"


# ── git fixtures ─────────────────────────────────────────────────────────────

def _git(repo: Path, *argv: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *argv],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, f"git {argv} failed: {cp.stderr}"
    return (cp.stdout or "").strip()


def _mk_repo_with_feature(tmp_path: Path) -> tuple[Path, str]:
    """Real repo: main with one commit, plus an ff-able feature commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", f"team-os/{TICKET}")
    (repo / "b.txt").write_text("feature\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "worker output")
    sha = _git(repo, "rev-parse", "HEAD")
    # Leave the checkout on the work branch — like the real repo, whose live
    # checkout is on a work branch while `main` is a plain (movable) ref.
    return repo, sha


def _diverge_main(repo: Path) -> None:
    here = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "main")
    (repo / "c.txt").write_text("diverged\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main moved on")
    _git(repo, "checkout", here)  # keep main un-checked-out, like the real repo


# ── kanban spine fixture ─────────────────────────────────────────────────────

def _mk_spine(tmp_path, monkeypatch, *, validator_done: bool = True):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kanban_db.connect(board=BOARD)
    worker_id = kanban_db.create_task(
        conn, title=f"{TICKET} worker", body="spine marker", assignee="team-os",
        created_by="test", workspace_kind="dir",
        idempotency_key=f"linear:{TICKET}:spine:worker", initial_status="running", board=BOARD,
    )
    validator_id = kanban_db.create_task(
        conn, title=f"{TICKET} validator", body="spine marker", assignee="team-os",
        created_by="test", workspace_kind="dir",
        idempotency_key=f"linear:{TICKET}:spine:validator", initial_status="running", board=BOARD,
    )
    if validator_done:
        kanban_db.complete_task(conn, str(validator_id), summary="PASS", metadata={})
    conn.close()
    return str(worker_id), str(validator_id)


# ── 1. record_landing_evidence ───────────────────────────────────────────────

def test_record_then_evidence_round_trip_with_real_repo(tmp_path, monkeypatch):
    """The whole point: what record writes, _landing_evidence must find."""
    repo, sha = _mk_repo_with_feature(tmp_path)
    _mk_spine(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "_REPOS", (str(repo),))

    rec = lw.record_landing_evidence(TICKET, PROJECT, sha, f"team-os/{TICKET}")
    assert rec["recorded"] is True

    conn = kanban_db.connect(board=BOARD)
    try:
        ev = lw._landing_evidence(conn, TICKET)
    finally:
        conn.close()
    assert ev == {"ok": True, "kind": "commit", "sha": sha, "repo": str(repo)}


def test_record_is_idempotent(tmp_path, monkeypatch):
    repo, sha = _mk_repo_with_feature(tmp_path)
    worker_id, _ = _mk_spine(tmp_path, monkeypatch)
    assert lw.record_landing_evidence(TICKET, PROJECT, sha)["recorded"] is True
    second = lw.record_landing_evidence(TICKET, PROJECT, sha)
    assert second["recorded"] is True and second["reason"] == "already recorded"
    conn = kanban_db.connect(board=BOARD)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ? AND body LIKE ?",
            (worker_id, f"%{sha}%"),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1  # no duplicate evidence comments


def test_record_refuses_garbage_sha_and_missing_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert lw.record_landing_evidence(TICKET, PROJECT, "not-a-sha")["recorded"] is False
    # valid sha shape but no spine chain exists on the board
    assert lw.record_landing_evidence(TICKET, PROJECT, "a" * 40)["recorded"] is False


# ── 2. _land_commit_locally ──────────────────────────────────────────────────

def test_land_fast_forwards_local_main(tmp_path):
    repo, sha = _mk_repo_with_feature(tmp_path)
    out = lw._land_commit_locally(str(repo), sha)
    assert out["landed"] is True
    assert _git(repo, "rev-parse", "main") == sha  # main actually moved
    # working tree untouched: still checked out on main's old content is fine;
    # the ref moved but no checkout/merge ran in the working tree
    assert _git(repo, "status", "--porcelain") == ""


def test_land_is_idempotent_when_already_on_main(tmp_path):
    repo, sha = _mk_repo_with_feature(tmp_path)
    assert lw._land_commit_locally(str(repo), sha)["landed"] is True
    again = lw._land_commit_locally(str(repo), sha)
    assert again["landed"] is True and "already on main" in again["reason"]


def test_land_refuses_non_fast_forward_loudly(tmp_path):
    repo, sha = _mk_repo_with_feature(tmp_path)
    _diverge_main(repo)  # main moved since the worker branched
    out = lw._land_commit_locally(str(repo), sha)
    assert out["landed"] is False
    assert "NOT fast-forward-able" in out["reason"]  # loud, no auto-resolve
    assert _git(repo, "rev-parse", "main") != sha  # main untouched


def test_land_refuses_when_main_is_checked_out(tmp_path):
    repo, sha = _mk_repo_with_feature(tmp_path)
    _git(repo, "checkout", "main")  # a live checkout ON main
    out = lw._land_commit_locally(str(repo), sha)
    assert out["landed"] is False
    assert "checked out" in out["reason"]  # clear refusal, not raw git stderr


def test_land_refuses_when_no_main_branch(tmp_path):
    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init", "-b", "trunk")
    (repo / "x").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "x")
    sha = _git(repo, "rev-parse", "HEAD")
    out = lw._land_commit_locally(str(repo), sha)
    assert out["landed"] is False and "no local 'main' branch" in out["reason"]


# ── 3. run_integrator_auto_land: the flag gate ───────────────────────────────

@pytest.fixture()
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr(lw, "_send_integrator_fyi", lambda *a, **k: {"sent": False, "reason": "test"})
    monkeypatch.setattr(lw, "_integrator_finalize_linear", lambda *a, **k: {"done": False, "reason": "test"})


def test_flag_off_lands_state_only_and_never_touches_git(tmp_path, monkeypatch, _quiet_side_effects):
    repo, sha = _mk_repo_with_feature(tmp_path)
    _mk_spine(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "_REPOS", (str(repo),))
    monkeypatch.delenv("TEAM_OS_INTEGRATOR_LAND_CODE", raising=False)
    lw.record_landing_evidence(TICKET, PROJECT, sha)
    main_before = _git(repo, "rev-parse", "main")

    out = lw.run_integrator_auto_land(TICKET, PROJECT)

    assert out["status"] == "auto_landed"  # current behavior preserved
    assert out["code_landing"]["landed"] is False
    assert "flag off" in out["code_landing"]["reason"]
    assert _git(repo, "rev-parse", "main") == main_before  # git untouched


def test_flag_on_lands_the_evidenced_commit_on_local_main(tmp_path, monkeypatch, _quiet_side_effects):
    repo, sha = _mk_repo_with_feature(tmp_path)
    _mk_spine(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "_REPOS", (str(repo),))
    monkeypatch.setenv("TEAM_OS_INTEGRATOR_LAND_CODE", "1")
    lw.record_landing_evidence(TICKET, PROJECT, sha)

    out = lw.run_integrator_auto_land(TICKET, PROJECT)

    assert out["status"] == "auto_landed"
    assert out["code_landing"]["landed"] is True
    assert _git(repo, "rev-parse", "main") == sha  # THE DRIVESHAFT: code arrived


def test_flag_on_unclean_landing_bounces_before_any_state_change(tmp_path, monkeypatch, _quiet_side_effects):
    repo, sha = _mk_repo_with_feature(tmp_path)
    worker_id, _ = _mk_spine(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "_REPOS", (str(repo),))
    monkeypatch.setenv("TEAM_OS_INTEGRATOR_LAND_CODE", "1")
    lw.record_landing_evidence(TICKET, PROJECT, sha)
    _diverge_main(repo)  # now the landing cannot fast-forward

    out = lw.run_integrator_auto_land(TICKET, PROJECT)

    assert out["status"] == "bounced_code_landing"
    assert "refusing to auto-resolve" in out["code_landing"]["reason"]
    conn = kanban_db.connect(board=BOARD)
    try:
        worker_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (worker_id,)
        ).fetchone()["status"]
        integrator = conn.execute(
            "SELECT 1 FROM tasks WHERE idempotency_key = ?",
            (f"linear:{TICKET}:spine:integrator",),
        ).fetchone()
    finally:
        conn.close()
    assert worker_status != "done"  # fail-closed: NO ceremony Done over unlanded code
    assert integrator is None       # no integrator marker created either


def test_flag_on_no_code_ticket_lands_state_without_git(tmp_path, monkeypatch, _quiet_side_effects):
    _mk_spine(tmp_path, monkeypatch)
    monkeypatch.setenv("TEAM_OS_INTEGRATOR_LAND_CODE", "1")
    conn = kanban_db.connect(board=BOARD)
    try:
        worker = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?", (f"linear:{TICKET}:spine:worker",)
        ).fetchone()
        kanban_db.add_comment(conn, str(worker["id"]), "test", "investigation-only, no code changes")
    finally:
        conn.close()

    out = lw.run_integrator_auto_land(TICKET, PROJECT)

    assert out["status"] == "auto_landed"
    assert out["code_landing"] == {"landed": True, "reason": "no-code ticket — nothing to land"}


# ── 4. the wire: callers record evidence for landable executions ────────────

def test_dispatch_worker_returns_its_branch(monkeypatch, tmp_path):
    from hermes_cli.team_os import worker_dispatch as wd
    out = wd.dispatch_worker(
        {"source_ticket": TICKET, "human_gate_required": False},
        enabled=True,
        runner=lambda **k: {"worker_status": "completed", "changed_files": [], "schema": "team_os.worker_handoff.v1"},
    )
    assert out["dispatched"] is True
    assert out["branch"] == f"team-os/{TICKET}"


def test_webhook_approved_records_evidence_for_landable_execution(tmp_path, monkeypatch):
    """Approved flow: a landable execute_spine result must be posted as landing
    evidence BEFORE the integrator runs (which reads it)."""
    from hermes_cli.team_os import worker_dispatch as wd
    from hermes_cli.team_os.db import TeamOSState

    sha = "f" * 40
    monkeypatch.setattr(lw, "_team_os_paused", lambda: False)
    monkeypatch.setattr(
        wd, "execute_spine",
        lambda contract, **k: {"ran": True, "landable": True, "commit": sha,
                               "worker": {"branch": f"team-os/{TICKET}"}, "validator": {"verdict": "PASS"}},
    )
    recorded: list[tuple] = []
    order: list[str] = []
    monkeypatch.setattr(
        lw, "record_landing_evidence",
        lambda ticket, project, s, branch="": (recorded.append((ticket, s, branch)), order.append("record"))[0] or {"recorded": True},
    )
    monkeypatch.setattr(
        lw, "unblock_approved_kanban_worker",
        lambda ticket, project: {"board": BOARD, "worker": "t", "unblocked": True},
    )

    state = TeamOSState(tmp_path / "t.db")
    event_id = state.queue_for_dispatch(
        event_type="linear_observation", source_id=TICKET, source="linear",
        payload={"source_id": TICKET, "title": "t", "project": PROJECT,
                 "cto_contract": {"source_ticket": TICKET, "human_gate_required": True}},
    )
    state.mark_event_mj_review(event_id, reason="human gate required")

    payload = {
        "action": "update", "type": "Issue", "webhookTimestamp": 1780930000000,
        "updatedFrom": {"state": {"name": "Needs-MJ"}},
        "data": {"id": "issue-uuid", "identifier": TICKET, "title": "t",
                 "url": f"https://linear.app/blazeragency/issue/{TICKET.lower()}",
                 "state": {"name": "Approved"}},
    }
    res = lw.handle_linear_webhook(
        payload, state=state, add_comment=lambda *a, **k: None,
        run_intake_wake=lambda **k: {"started": True},
        run_integrator_auto_land=lambda **k: order.append("integrator") or {"status": "auto_landed"},
    )

    assert res.get("decision") == "approved"
    assert recorded == [(TICKET, sha, f"team-os/{TICKET}")]  # evidence recorded
    assert order == ["record", "integrator"]  # …and BEFORE the integrator ran
