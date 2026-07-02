"""Deterministic Team OS dispatcher wiring.

Connects one Cortex outbox event to the existing ephemeral Worker runner and
cold Validator runner.  This module deliberately contains policy gates in code:
only low-failure-cost internal tickets may dispatch automatically, and auto-Done
is opt-in after a clean Validator PASS.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .clarification_gate import assess_clarification_need, build_clarification_card
from .contracts import check_contract
from .integrator import IntegratorInput, IntegratorResult, integrate_after_validator
from .validator_runner import run_validator
from .worker_runner import run_worker

WorkerCallable = Callable[..., dict[str, Any]]
ValidatorCallable = Callable[..., dict[str, Any]]
IntegratorCallable = Callable[[IntegratorInput], IntegratorResult]
TelegramPush = Callable[[str], None]
AssignMJ = Callable[[str], None]
AutoDone = Callable[[str], None]

_BLOCKED_SURFACE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcustomer[_ -]?infra(structure)?\b", re.IGNORECASE), "customer_infra_writes"),
    (re.compile(r"\bmoney\b|\bbilling\b|\bpayment\b|\binvoice\b", re.IGNORECASE), "money/billing"),
    (re.compile(r"\bclient\b|\bcustomer-facing\b", re.IGNORECASE), "client-facing"),
    (re.compile(r"\bprod(uction)?\b|\bdeploy\b|\brelease\b", re.IGNORECASE), "prod/production"),
    (re.compile(r"\bcredential\b|\bsecret\b|\btoken\b|\bapi[-_ ]?key\b", re.IGNORECASE), "credentials/secrets"),
    (re.compile(r"\bklaviyo\b|\bsend\b|\bemail campaign\b", re.IGNORECASE), "external send surface"),
)


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime config for one deterministic outbox dispatch."""

    repo_root: Path
    worktree_root: Path
    artifact_root: Path
    lease_root: Path
    worker_timeout_seconds: float = 600.0
    telegram_push_enabled: bool = False
    auto_done_low_cost: bool = False
    integrator_auto_land: bool = False
    integrator_main_branch: str = "main"
    integrator_deploy_command: tuple[str, ...] = ()
    integrator_fyi_counter_path: Path | None = None
    integrator_fyi_limit: int = 3


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return slug or "team-os-ticket"


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {})
    return payload if isinstance(payload, dict) else {}


def _source_ticket(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return str(payload.get("source_id") or event.get("source_id") or "unknown")


def _blocked_surface_reason(event: dict[str, Any], contract: dict[str, Any]) -> str | None:
    payload = _event_payload(event)
    parts: list[str] = [
        str(payload.get("title") or ""),
        str(payload.get("body") or ""),
        " ".join(str(x) for x in payload.get("labels", []) if isinstance(x, str)),
        " ".join(str(x) for x in contract.get("files_to_touch", []) if isinstance(x, str)),
    ]
    text = "\n".join(parts)
    for pattern, label in _BLOCKED_SURFACE_PATTERNS:
        if pattern.search(text):
            return f"blocked surface: {label}"
    return None


def _is_low_failure_cost(event: dict[str, Any]) -> bool:
    payload = _event_payload(event)
    return (
        str(payload.get("failure_cost_tier") or "").lower() == "low"
        and bool(payload.get("requires_mj_review")) is False
        and str(payload.get("gate") or "").lower() != "human"
        and bool(payload.get("classifier_uncertain")) is False
    )


def _requires_human_gate(event: dict[str, Any]) -> bool:
    payload = _event_payload(event)
    labels = [str(x).lower() for x in payload.get("labels", []) if isinstance(x, str)]
    tier = str(payload.get("failure_cost_tier") or "unknown").lower()
    return (
        tier != "low"
        or bool(payload.get("requires_mj_review"))
        or str(payload.get("gate") or "").lower() == "human"
        or bool(payload.get("classifier_uncertain"))
        or "gate:human" in labels
    )


def _build_contract(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    supplied = payload.get("validation_contract")
    if isinstance(supplied, dict):
        contract = dict(supplied)
    else:
        ticket = _source_ticket(event)
        title = str(payload.get("title") or ticket)
        body = str(payload.get("body") or "")
        tier = str(payload.get("failure_cost_tier") or "low")
        contract = {
            "source_ticket": ticket,
            "intended_behavior": f"Execute Linear ticket {ticket}: {title}",
            "non_goals": [
                "Do not touch production, money, client, credential, or customer infrastructure surfaces",
                "Do not mark Linear Done from the Worker",
            ],
            "assertions": [
                "Worker produces a focused diff for the source ticket",
                "Worker proof commands complete successfully",
                "Validator returns PASS before any status transition",
            ],
            "commands": ["git diff --stat"],
            "required_commands": ["git diff --stat"],
            "behavior_check_required": True,
            "risk": "low" if tier == "low" else "medium",
            "human_gate_required": True,
            "bounce_conditions": [
                "Worker touches blocked surfaces",
                "Proof is missing or irrelevant",
                "Validator returns BOUNCE",
            ],
            "files_to_touch": list(payload.get("files_to_touch", []))
            if isinstance(payload.get("files_to_touch"), list)
            else [],
            "linear_title": title,
            "linear_body": body,
            "linear_url": payload.get("url"),
        }
    contract.setdefault("source_ticket", _source_ticket(event))
    contract.setdefault("required_commands", contract.get("commands", []))
    return contract


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _proof_commands_passed(worker_result: dict[str, Any]) -> bool:
    proof = worker_result.get("proof_results")
    if not isinstance(proof, list) or not proof:
        return False
    for item in proof:
        if not isinstance(item, dict) or int(item.get("exit_code", 1)) != 0:
            return False
    return True


def _changed_files_within_contract(worker_result: dict[str, Any], contract: dict[str, Any]) -> str | None:
    allowed = [str(x) for x in contract.get("files_to_touch", []) if isinstance(x, str) and x.strip()]
    changed = [str(x) for x in worker_result.get("changed_files", []) if isinstance(x, str)]
    if not allowed:
        return None
    extra = sorted(set(changed) - set(allowed))
    if extra:
        return "changed files outside contract: " + ", ".join(extra)
    return None


def _proof_link(event: dict[str, Any], ticket: str) -> str:
    payload = _event_payload(event)
    url = str(payload.get("url") or "").strip()
    if url:
        return url
    return f"https://linear.app/blazeragency/issue/{ticket.lower()}"


def _human_gate_message(event: dict[str, Any], ticket: str, reason: str) -> str:
    payload = _event_payload(event)
    title = str(payload.get("title") or ticket).strip()
    return (
        f"Team OS human gate: {ticket} needs MJ review. "
        f"Reason: {reason}. Proof: {_proof_link(event, ticket)}. Title: {title}"
    )


def dispatch_outbox_event(
    event: dict[str, Any],
    config: DispatcherConfig,
    *,
    worker: WorkerCallable | None = None,
    validator: ValidatorCallable | None = None,
    integrator: IntegratorCallable | None = None,
    telegram_push: TelegramPush | None = None,
    assign_mj: AssignMJ | None = None,
    auto_done: AutoDone | None = None,
) -> dict[str, Any]:
    """Dispatch one queued outbox event through Worker then Validator.

    Returns a structured artifact suitable for outbox/Linear proof.  Raises no
    exception for policy denials; callers may mark the outbox event failed or
    held based on the returned ``status``.
    """
    ticket = _source_ticket(event)
    event_id = int(event.get("id") or 0)
    contract = _build_contract(event)
    tier = str(_event_payload(event).get("failure_cost_tier") or "unknown").lower()
    base: dict[str, Any] = {
        "source_ticket": ticket,
        "event_id": event_id,
        "failure_cost_tier": tier,
        "status": "started",
        "telegram_push": {"enabled": config.telegram_push_enabled, "sent": False},
        "auto_done": {"enabled": config.auto_done_low_cost, "attempted": False, "done": False},
    }

    if _requires_human_gate(event):
        result = {
            **base,
            "status": "needs_mj",
            "reason": "human gate required",
            "proof_link": _proof_link(event, ticket),
            "linear_assignment": {"assignee": "MJ", "set": False},
        }
        if assign_mj is None:
            result["linear_assignment"] = {"assignee": "MJ", "set": False, "reason": "assignment callback not configured"}
            result["telegram_push"] = {"enabled": config.telegram_push_enabled, "sent": False}
            return result
        assign_mj(ticket)
        result["linear_assignment"] = {"assignee": "MJ", "set": True}
        if config.telegram_push_enabled:
            if telegram_push is None:
                result["telegram_push"] = {"enabled": True, "sent": False, "reason": "telegram callback not configured"}
            else:
                telegram_push(_human_gate_message(event, ticket, "human gate required"))
                result["telegram_push"] = {"enabled": True, "sent": True}
        return result

    blocked_reason = _blocked_surface_reason(event, contract)
    if blocked_reason:
        return {**base, "status": "blocked_surface", "reason": blocked_reason}

    contract_errors = check_contract(contract)
    if contract_errors:
        return {**base, "status": "invalid_contract", "contract_errors": contract_errors}

    clarification = assess_clarification_need(_event_payload(event), contract)
    if clarification.needs_clarification:
        return {
            **base,
            "status": "clarification_needed",
            "lane": clarification.lane,
            "reason": "; ".join(clarification.reasons),
            "structured_asks": list(clarification.asks),
            "clarification_card": build_clarification_card(ticket, clarification),
        }

    if not _is_low_failure_cost(event):
        return {**base, "status": "blocked_failure_cost", "reason": "dispatch requires low failure-cost and no MJ-review hold"}

    slug = _safe_slug(ticket)
    branch = f"team-os-{slug}-{event_id or 'manual'}"
    artifact_dir = config.artifact_root / slug / str(event_id or "manual")
    contract_path = artifact_dir / "contract.json"
    handoff_path = artifact_dir / "handoff.json"
    validator_path = artifact_dir / "validator.json"
    _write_json(contract_path, contract)

    worker_fn = worker or run_worker
    worker_result = worker_fn(
        contract=contract,
        repo_root=config.repo_root,
        worktree_root=config.worktree_root,
        lease_path=config.lease_root / f"{slug}.json",
        branch=branch,
        timeout_seconds=config.worker_timeout_seconds,
    )
    _write_json(handoff_path, worker_result)
    base.update(
        {
            "contract_path": str(contract_path),
            "handoff_path": str(handoff_path),
            "worker": worker_result,
        }
    )

    if worker_result.get("worker_status") != "completed":
        return {**base, "status": "worker_failed", "reason": str(worker_result.get("error") or worker_result.get("fallback_reason") or worker_result.get("worker_status"))}
    if not _proof_commands_passed(worker_result):
        return {**base, "status": "proof_failed", "reason": "worker proof results missing or non-zero"}
    scope_error = _changed_files_within_contract(worker_result, contract)
    if scope_error:
        return {**base, "status": "worker_scope_denied", "reason": scope_error}

    validator_fn = validator or run_validator
    validator_result = validator_fn(
        contract_path=contract_path,
        handoff_path=handoff_path,
        state_path=config.artifact_root / "validator-bounces.json",
    )
    _write_json(validator_path, validator_result)
    base.update({"validator": validator_result, "validator_path": str(validator_path)})

    if validator_result.get("verdict") != "PASS":
        return {**base, "status": "validator_bounced", "reason": "validator did not PASS"}

    result = {**base, "status": "validated"}

    if config.integrator_auto_land:
        integrator_input = IntegratorInput(
            source_ticket=ticket,
            worktree_path=Path(str(worker_result.get("worktree_path") or config.worktree_root / branch)),
            handoff_path=handoff_path,
            validator_report_path=validator_path,
            main_branch=config.integrator_main_branch,
            # AGENTS-297: dispatcher must not smuggle deploy authority into the
            # default Integrator path.  Remote/deploy is out of scope for the
            # canonical local-only Integrator and must be a separate hard gate.
            deploy_command=(),
            fyi_counter_path=config.integrator_fyi_counter_path,
            fyi_limit=config.integrator_fyi_limit,
        )
        integrator_fn = integrator or integrate_after_validator
        integrated = integrator_fn(integrator_input)
        result["integrator"] = {
            "status": integrated.status,
            "reversibility": integrated.reversibility,
            "reason": integrated.reason,
            "rollback_commands": list(integrated.rollback_commands),
            "fyi_sent": integrated.fyi_sent,
            "commands": [list(cmd) for cmd in integrated.commands],
            "gate_card": integrated.gate_card,
        }
        if integrated.status in {"auto_landed", "needs_mj"}:
            result["status"] = integrated.status
        else:
            return {**result, "status": "integrator_blocked", "reason": integrated.reason}

    if config.auto_done_low_cost:
        result["auto_done"]["attempted"] = True
        if auto_done is None:
            result["auto_done"]["reason"] = "auto-Done callback not configured"
        else:
            auto_done(ticket)
            result["auto_done"]["done"] = True
    return result
