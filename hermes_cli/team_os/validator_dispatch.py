"""validator_dispatch.py — connect the real Validator (Stage D of WIRE-THE-REAL-ROAD).

The Validator engine (validator_runner.run_validator) exists and is tested:
a cold, adversarial 5-step review of the Worker handoff against the contract,
returning VERDICT: PASS|BOUNCE with bounce tracking and MJ escalation at >=2
bounces. It is never invoked by the live flow. This flag-gated connector runs
it on a worker's (contract, handoff) and returns the gate result.

Safety / rollout:
  • OFF by default (``TEAM_OS_VALIDATOR_DISPATCH``); flag-off → not run.
  • FAIL-CLOSED: any error → verdict BOUNCE (never let unreviewed work pass
    because the validator crashed).
  • CROSS-MODEL by default: the verifier runs on CODEX (gpt-5.5 via the ChatGPT
    subscription) while the Worker runs on opus (Claude) — a genuinely different
    model, so the verifier doesn't share the builder's blind spots. Callers can
    still inject their own reviewer/review_cmd to override.
  • No board writes / side effects — returns a verdict dict only.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

Reviewer = Callable[[str], str]

_STATE = Path.home() / ".hermes" / "state"
_BOUNCE_STATE = _STATE / "team-os-validator-bounce.json"
_HERMES = os.environ.get("HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes"))


def codex_reviewer(prompt: str, *, timeout: float = 120.0) -> str:
    """CROSS-MODEL verifier: run the validation on CODEX (gpt-5.5 via the ChatGPT
    subscription) — a DIFFERENT model than the Worker (Claude/opus), so the
    verifier does not share the builder's blind spots (true Builder≠Verifier
    independence, per the over-engineering-gate plan). No tools — pure judgment.

    Returns the model's text (the validator parses 'VERDICT: PASS|BOUNCE' from
    it). Returns '' on any error → the validator fails CLOSED to BOUNCE, so a
    codex outage over-blocks rather than rubber-stamps. Never raises."""
    import subprocess
    try:
        proc = subprocess.run(
            [_HERMES, "chat", "-Q", "--provider", "openai-codex", "-m", "gpt-5.5",
             "--no-tools", "-q", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
            # cron/launchd give a minimal PATH that lacks ~/.local/bin — make it explicit.
            env={**os.environ, "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")},
        )
        # strip the 'session_id:' line + blanks (same shape the cron scripts expect)
        lines = [l for l in (proc.stdout or "").splitlines()
                 if not l.startswith("session_id:") and l.strip()]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - verifier failure must fail closed, never raise
        return ""


def dispatch_validator(
    contract: dict[str, Any],
    handoff: dict[str, Any],
    *,
    reviewer: Reviewer | None = None,
    review_cmd: Sequence[str] | None = None,
    state_path: Path | None = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    """Run the real Validator on a worker handoff. Never raises.

    Returns:
        {"validated": bool, "verdict": "PASS"|"BOUNCE", "bounce_count": int,
         "escalate_mj": bool, "reason": str}
    """
    ticket = str(contract.get("source_ticket") or handoff.get("source_ticket") or "AGENTS-?")

    def _bounce(reason: str, *, validated: bool = False) -> dict[str, Any]:
        return {
            "validated": validated, "verdict": "BOUNCE", "bounce_count": -1,
            "escalate_mj": False, "ticket": ticket, "reason": reason,
        }

    if enabled is None:
        enabled = _flag_on(os.environ.get("TEAM_OS_VALIDATOR_DISPATCH"))
    if not enabled:
        return _bounce("validator dispatch disabled (TEAM_OS_VALIDATOR_DISPATCH off)")

    try:
        from .validator_runner import run_validator
    except Exception as exc:  # noqa: BLE001
        return _bounce(f"validator engine import failed: {str(exc)[:160]}")

    sp = Path(state_path) if state_path else _BOUNCE_STATE
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="tos-validate-"))
    try:
        cpath = tmp / "contract.json"
        hpath = tmp / "handoff.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        hpath.write_text(json.dumps(handoff), encoding="utf-8")
        # Default verifier = CODEX (cross-model from the opus Worker). Callers can
        # still inject a reviewer/review_cmd (tests do); only when neither is given
        # do we use codex instead of the engine's same-model opus default.
        if reviewer is None and review_cmd is None:
            reviewer = codex_reviewer
        result = run_validator(
            contract_path=cpath, handoff_path=hpath, state_path=sp,
            reviewer=reviewer, review_cmd=review_cmd,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _bounce(f"validator run error: {str(exc)[:200]}")
    finally:
        try:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()
        except OSError:
            pass

    if not isinstance(result, dict) or result.get("verdict") not in {"PASS", "BOUNCE"}:
        return _bounce("validator returned no usable verdict")
    return {
        "validated": True,
        "verdict": result["verdict"],
        "bounce_count": int(result.get("bounce_count", 0)),
        "escalate_mj": bool(result.get("escalate_mj", False)),
        "ticket": ticket,
        "reason": "validator ran",
    }


def _flag_on(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}
