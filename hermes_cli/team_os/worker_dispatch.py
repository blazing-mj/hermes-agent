"""worker_dispatch.py — connect the REAL Worker engine (Stage C of WIRE-THE-REAL-ROAD).

The Worker engine (worker_runner.run_worker) already exists and is tested:
isolated git worktree, claude-max-code, proof commands, handoff with a real
commit. It just is never invoked by the live flow. This is the thin, flag-gated
connector that the flow calls to actually run the Worker on a CTO contract.

Safety / rollout:
  • OFF by default. Only dispatches when ``TEAM_OS_WORKER_DISPATCH`` is truthy.
    Flag-off → returns {"dispatched": False, ...}; the live flow runs nothing.
  • GATE ENFORCED HERE: a contract with human_gate_required=True is NEVER
    auto-dispatched (that work waits for MJ). Only reversible, non-gated
    contracts can run.
  • Isolated: the Worker runs in a throwaway git worktree + branch under
    ~/.hermes/state/team-os-worktrees, never on the live checkout's working
    tree. Nothing is pushed; landing stays a separate gated step (Integrator).
  • FAIL-SAFE: any error → {"dispatched": False, "reason": ...}; never raises.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

# Runner signature mirrors worker_runner.run_worker (kwargs). Injectable for tests.
WorkerRunner = Callable[..., dict[str, Any]]

_STATE = Path.home() / ".hermes" / "state"
_DEFAULT_REPO = Path.home() / ".hermes" / "hermes-agent"
_WORKTREE_ROOT = _STATE / "team-os-worktrees"
_LEASE_ROOT = _STATE / "team-os-leases"


def dispatch_worker(
    contract: dict[str, Any],
    *,
    repo_root: Path | str | None = None,
    runner: WorkerRunner | None = None,
    enabled: Optional[bool] = None,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Run the real Worker on a contract, isolated + gated. Never raises.

    Returns:
        {"dispatched": bool, "reason": str, "handoff": {...}?}
    where handoff is the worker_runner result (changed_files, proof_results,
    worker_status, commit) when a run actually happened.
    """
    ticket = str(contract.get("source_ticket") or "AGENTS-?")

    def _no(reason: str, **extra: Any) -> dict[str, Any]:
        return {"dispatched": False, "reason": reason, "ticket": ticket, **extra}

    # Gate: never auto-run human-gated (consequential/irreversible) work.
    if contract.get("human_gate_required") is True:
        return _no("human-gate-required — waits for MJ approval, not auto-dispatched")

    if enabled is None:
        enabled = _flag_on(os.environ.get("TEAM_OS_WORKER_DISPATCH"))
    if not enabled:
        return _no("worker dispatch disabled (TEAM_OS_WORKER_DISPATCH off)")

    run = runner
    if run is None:
        try:
            from .worker_runner import run_worker as run
        except Exception as exc:  # noqa: BLE001
            return _no(f"worker engine import failed: {str(exc)[:160]}")

    repo = Path(repo_root) if repo_root else _DEFAULT_REPO
    branch = f"team-os/{ticket}".replace("\\", "-")
    lease_path = _LEASE_ROOT / f"{ticket.replace('/', '-')}.lease"
    _WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    _LEASE_ROOT.mkdir(parents=True, exist_ok=True)

    try:
        handoff = run(
            contract=contract,
            repo_root=repo,
            worktree_root=_WORKTREE_ROOT,
            lease_path=lease_path,
            branch=branch,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - engine failure must not break the flow
        return _no(f"worker run error: {str(exc)[:200]}")

    if not isinstance(handoff, dict):
        return _no("worker returned no handoff")
    status = handoff.get("worker_status")
    changed = handoff.get("changed_files", [])

    # run_worker leaves changes UNCOMMITTED in the worktree (verified: it never
    # commits). The Integrator's no-empty-landing gate needs a real commit sha,
    # so capture the worker's output as a commit on its throwaway branch now.
    # This is NOT a land: the branch is never pushed; the Integrator only lands
    # after the Validator passes + MJ's gate. A bounce just abandons the branch.
    commit_sha = None
    commit_error = None
    if status == "completed" and changed:
        worktree_path = _WORKTREE_ROOT / branch.replace("/", "-")
        commit_sha, commit_error = _commit_worktree(worktree_path, ticket)

    return {
        "dispatched": True,
        "ticket": ticket,
        "worker_status": status,
        "changed_files": changed,
        "commit": commit_sha,
        "commit_error": commit_error,
        "branch": branch,
        "handoff": handoff,
    }


def _commit_worktree(worktree_path: Path, ticket: str) -> tuple[Optional[str], Optional[str]]:
    """git add -A + commit the worker's output in its worktree. Returns
    (sha, None) on success or (None, error). Never raises."""
    try:
        add = subprocess.run(["git", "-C", str(worktree_path), "add", "-A"],
                             capture_output=True, text=True, timeout=30)
        if add.returncode != 0:
            return None, f"git add failed: {(add.stderr or '').strip()[:160]}"
        commit = subprocess.run(
            ["git", "-C", str(worktree_path), "commit",
             "-m", f"team-os: {ticket} worker output (pre-validation, not landed)"],
            capture_output=True, text=True, timeout=30,
        )
        if commit.returncode != 0:
            return None, f"git commit failed: {(commit.stderr or commit.stdout or '').strip()[:160]}"
        sha = subprocess.run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return (sha or None), None
    except Exception as exc:  # noqa: BLE001
        return None, f"commit error: {str(exc)[:160]}"


def execute_spine(contract: dict[str, Any], *, repo_root: Path | str | None = None) -> dict[str, Any]:
    """The autonomous execution slice: run the Worker, and IF it produced a real
    commit, run the Validator on its handoff.

    This is the wire that connects the built-but-dormant agents into the live
    flow. It changes NOTHING until both connectors' flags are on AND TeamOS is
    unpaused, because:
      - dispatch_worker is OFF unless TEAM_OS_WORKER_DISPATCH (and refuses
        human-gated contracts), and
      - dispatch_validator is OFF unless TEAM_OS_VALIDATOR_DISPATCH.
    So with the flags off (default), this returns ran=False and the live flow is
    byte-for-byte unchanged. Never raises.

    Returns: {ran, worker, validator, commit, landable, reason} where
    landable = a real commit exists AND the Validator returned PASS.
    """
    from .validator_dispatch import dispatch_validator

    w = dispatch_worker(contract, repo_root=repo_root)
    if not w.get("dispatched"):
        return {"ran": False, "worker": w, "validator": None,
                "commit": None, "landable": False, "reason": w.get("reason")}
    if w.get("worker_status") != "completed":
        return {"ran": True, "worker": w, "validator": None, "commit": w.get("commit"),
                "landable": False, "reason": f"worker status {w.get('worker_status')}"}
    v = dispatch_validator(contract, w.get("handoff") or {})
    landable = bool(w.get("commit")) and v.get("verdict") == "PASS"
    return {"ran": True, "worker": w, "validator": v, "commit": w.get("commit"),
            "landable": landable,
            "reason": "validated PASS" if landable else f"validator {v.get('verdict')}"}


def _flag_on(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}
