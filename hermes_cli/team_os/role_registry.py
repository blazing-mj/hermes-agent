"""Machine-readable Team OS role/capability registry.

This is the dispatch fail-closed surface for the "why Ruta?" class of bug:
a profile merely existing on disk is not permission to receive arbitrary Kanban
work.  Runtime/profile roles declare what they may receive; validator work is
pinned to the cold Claude Max rail and must never be improvised as a gateway
profile assignment.
"""

from __future__ import annotations

from typing import Any, Optional

# Keep this plain data so tests/tools can serialize/read it directly.
ROLE_CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "default": {
        "dispatchable": True,
        "task_types": ["implementation", "ops", "docs", "research", "triage"],
    },
    "cortex": {
        "dispatchable": True,
        "task_types": ["grounding", "contract", "gate", "triage"],
        "not_task_types": ["implementation", "validator"],
    },
    "cto": {
        "dispatchable": True,
        "task_types": ["route", "contract", "triage"],
        "not_task_types": ["validator"],
    },
    "ruta": {
        "dispatchable": False,
        "task_types": ["chat"],
        "reason": "ruta is a chat assistant profile, not a Team OS dispatch worker",
    },
    "billprinter": {
        "dispatchable": False,
        "task_types": ["trading"],
        "reason": "billprinter is a trader profile, not a Team OS dispatch worker",
    },
    # Validator work is not a Hermes gateway profile.  These lane names are
    # deliberately non-profile assignees; the gateway dispatcher skips them and
    # the external cold-rail runner owns execution.
    "claude-max-code": {
        "dispatchable": False,
        "task_types": ["validator"],
        "external_runner": "run_adversarial_validator",
        "reason": "validator cards must run on the cold Claude Max rail, not a gateway profile",
    },
    "run_adversarial_validator": {
        "dispatchable": False,
        "task_types": ["validator"],
        "external_runner": "claude-max-code",
        "reason": "validator cards must run on the cold Claude Max rail, not a gateway profile",
    },
}

VALIDATOR_RAIL_ASSIGNEES = frozenset({"claude-max-code", "run_adversarial_validator"})
NON_DISPATCHABLE_PROFILES = frozenset(
    name for name, spec in ROLE_CAPABILITY_REGISTRY.items() if not spec.get("dispatchable")
)

_VALIDATOR_TOKENS = frozenset(
    {
        "validator",
        "validate",
        "validation",
        "verifier",
        "adversarial review",
        "independent proof",
        "cold review",
    }
)


def task_type_for(*, title: Optional[str], body: Optional[str], assignee: Optional[str]) -> str:
    """Classify a Kanban card into a coarse registry task type."""
    haystack = " ".join(part or "" for part in (title, body, assignee)).casefold()
    if any(token in haystack for token in _VALIDATOR_TOKENS):
        return "validator"
    if "contract" in haystack or "cto" in haystack:
        return "contract"
    if "ground" in haystack or "cortex" in haystack:
        return "grounding"
    if "route" in haystack or "triage" in haystack:
        return "triage"
    return "implementation"


def assignment_violation(*, title: Optional[str], body: Optional[str], assignee: Optional[str]) -> Optional[str]:
    """Return a clear fail-closed reason when assignment is out of registry."""
    if not assignee:
        return None
    profile = str(assignee).strip().casefold()
    # "team-os" is the CONTROL-PLANE marker label (dispatcher-jam fix): spine
    # markers carry it precisely so the kanban dispatcher SKIPS them — they are
    # completed by the Team OS state machine and never executed by any rail.
    # Rail-routing rules govern EXECUTABLE work, so they cannot apply here; the
    # registry rejecting the motor's own "<ticket> Validator independent proof"
    # marker crashed spine-chain creation (found by the P0 landing tests).
    # Safe: a REAL validator task mis-assigned to team-os could never run at
    # all (dispatcher skips it) — it parks visibly instead of running off-rail.
    if profile == "team-os":
        return None
    task_type = task_type_for(title=title, body=body, assignee=profile)

    if task_type == "validator":
        if profile in VALIDATOR_RAIL_ASSIGNEES:
            return None
        return (
            "role registry rejected assignment: validator task type must route to "
            "cold Claude Max rail (run_adversarial_validator / claude-max-code), "
            f"not gateway profile '{profile}'"
        )

    spec = ROLE_CAPABILITY_REGISTRY.get(profile)
    if spec and spec.get("dispatchable") is False:
        return (
            "role registry rejected assignment: "
            f"profile '{profile}' is not dispatchable ({spec.get('reason') or 'no dispatch capability'})"
        )
    if spec:
        denied = {str(x).casefold() for x in spec.get("not_task_types", [])}
        allowed = {str(x).casefold() for x in spec.get("task_types", [])}
        if task_type in denied:
            return (
                "role registry rejected assignment: "
                f"profile '{profile}' is forbidden for task type '{task_type}'"
            )
        if allowed and task_type not in allowed:
            return (
                "role registry rejected assignment: "
                f"profile '{profile}' allows {sorted(allowed)}, not task type '{task_type}'"
            )
    return None


def validator_contract_route() -> dict[str, Any]:
    """Pinned CTO contract snippet for validator routing."""
    return {
        "validator_route": "run_adversarial_validator",
        "validator_runner": "claude-max-code",
        "gateway_profiles_allowed": False,
        "must_not_assign_profiles": sorted(
            name for name, spec in ROLE_CAPABILITY_REGISTRY.items()
            if name not in VALIDATOR_RAIL_ASSIGNEES and spec.get("dispatchable") is False
        ),
    }
