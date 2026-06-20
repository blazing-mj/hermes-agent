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
  • Independent session: the validator runs as its own claude rail invocation,
    separate from the worker's session. For true CROSS-MODEL review, point the
    validator at a different model than the worker via ``review_cmd`` — the
    worker uses opus, so a different validator model is the stronger setup.
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
