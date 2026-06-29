"""cto_agent.py — the REAL CTO contract-scoper (Stage B of WIRE-THE-REAL-ROAD).

After Cortex audits a ticket (cortex_agent), the CTO must turn that grounding
into a concrete, scoped CONTRACT the Worker executes against and the Validator
checks against. Today the "cto" spine step just stamps a static template. This
module produces a REAL contract via an LLM:

  • intended_behavior — what the change should accomplish, specifically
  • files_to_touch    — the concrete files/areas in scope (worker boundary)
  • non_goals         — explicit out-of-scope to prevent scope creep
  • assertions        — acceptance criteria the Validator will check
  • bounce_conditions — when the Worker/Validator must stop
  • risk + human_gate — derived from Cortex's gated verdict (fail closed)

Safety / rollout:
  • OFF by default. Only used when ``TEAM_OS_CTO_AGENT`` is truthy AND a
    reviewer is wired. The live flow is unchanged until turn-on.
  • FAIL-SAFE: any error (rail down, bad/invalid contract) falls back to the
    deterministic worker template, so the flow can never break here.
  • The contract is validated with contracts.check_contract() before use;
    an invalid LLM contract is rejected → template fallback.
  • If Cortex gated the ticket, human_gate_required is forced True regardless
    of what the model returns (fail closed).
  • No board writes / side effects — returns a contract dict only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .contracts import check_contract, render_template

_CLAUDE_MAX_CODE = os.environ.get(
    "CLAUDE_MAX_CODE", str(Path.home() / ".hermes" / "bin" / "claude-max-code")
)

Reviewer = Callable[[str], str]

_CTO_SYSTEM = (
    "You are the Team OS CTO. Cortex has triaged a Linear ticket; your job is to "
    "turn it into a CONCRETE, MINIMAL implementation contract that a Worker will "
    "execute in an isolated worktree and a Validator will check. You do NOT "
    "implement anything — you scope it.\n\n"
    "The single most important field is definition_of_done. A WEAK contract tells "
    "the Worker to 'implement the plan'. A STRONG contract gives it something to be "
    "TESTED against — expected behavior, a success check, a clear standard for done. "
    "So:\n"
    "- definition_of_done = the ONE concrete, runnable check that proves this ticket "
    "is complete — ideally a command whose pass/fail is binary. When the code in "
    "scope ALREADY has tests, or the ticket is a bug with a reproducible failure, "
    "make the done-check 'these exact tests / this exact repro now pass' and NAME the "
    "command. A goal you can run beats a goal you can only read.\n"
    "- commands = the REAL proof commands that demonstrate done (the existing test "
    "command, the failing repro that must now go green, the lint). NO generic "
    "placeholders — if you cannot name a real proof command, the scope is still too "
    "vague; tighten it until you can.\n"
    "- assertions = the remaining acceptance criteria the Validator verifies, beyond "
    "the done-check. Concrete and checkable.\n"
    "Rules:\n"
    "- Keep scope minimal and reversible. List the SPECIFIC files/areas to touch.\n"
    "- non_goals = explicit out-of-scope to stop the Worker drifting.\n"
    "- bounce_conditions = when the Worker/Validator must STOP (definition_of_done "
    "not met, test fails, lint errors, kill-switch active, scope creep, proof missing).\n"
    "- risk = one of low|medium|high|critical.\n"
    "- Use NO tools. Do not investigate the codebase. Scope ONLY from the ticket "
    "and Cortex grounding below; respond with the JSON immediately.\n\n"
    "Output ONLY a single JSON object, no prose, with exactly these keys:\n"
    '{"intended_behavior": str, "definition_of_done": str, "files_to_touch": [str], '
    '"non_goals": [str], "assertions": [str], "commands": [str], '
    '"bounce_conditions": [str], "risk": "low|medium|high|critical", '
    '"behavior_check_required": true, "human_gate_required": bool, "scope_summary": str}'
)


def build_cto_prompt(payload: dict[str, Any], cortex_verdict: dict[str, Any] | None) -> str:
    """Render the contract-scoping prompt from the ticket + Cortex grounding."""
    ticket = str(payload.get("identifier") or payload.get("id") or "?")
    cv = cortex_verdict or {}
    grounding = cv.get("grounding_summary") or cv.get("root_cause") or "(no Cortex grounding)"
    return "\n".join([
        _CTO_SYSTEM,
        "",
        f"TICKET: {ticket}",
        f"TITLE: {payload.get('title') or '(none)'}",
        f"PROJECT: {payload.get('project') or '(none)'}",
        "DESCRIPTION:",
        str(payload.get("body") or payload.get("description") or "(empty)"),
        "",
        "CORTEX GROUNDING:",
        f"  decision: {cv.get('decision', '?')}  system: {cv.get('system', '?')}  "
        f"severity: {cv.get('severity', '?')}",
        f"  root_cause: {cv.get('root_cause', '?')}",
        f"  grounding: {grounding}",
        "",
        "Return the contract JSON now.",
    ])


def _default_reviewer(prompt: str, *, timeout: float = 150.0) -> str:
    result = subprocess.run(
        (
            _CLAUDE_MAX_CODE, "team-os-cto-agent", "--",
            "-p", prompt, "--model", "opus", "--max-turns", "2",
            "--disallowedTools",
            "Bash,Read,Edit,Write,Glob,Grep,LS,Task,TodoWrite,WebFetch,WebSearch",
        ),
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    return (result.stdout or "").strip()


def _parse_contract(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    obj: Any = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        return _parse_contract(obj["result"])
    return obj if isinstance(obj, dict) else None


def cto_contract(
    payload: dict[str, Any],
    cortex_verdict: dict[str, Any] | None = None,
    *,
    reviewer: Reviewer | None = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    """Produce a scoped, validated contract for one ticket. Never raises.

    Returns a contract dict that always passes contracts.check_contract(),
    tagged with ``contract_source`` = "cto-agent" (real) or
    "template-fallback" (agent off / errored / produced an invalid contract).
    If Cortex gated the ticket, human_gate_required is forced True.
    """
    ticket = str(payload.get("identifier") or payload.get("id") or "AGENTS-?")
    gated = bool((cortex_verdict or {}).get("gated"))

    def _fallback(reason: str) -> dict[str, Any]:
        contract = render_template("worker")
        contract["source_ticket"] = ticket
        contract["human_gate_required"] = True if gated else contract.get("human_gate_required", False)
        contract["contract_source"] = "template-fallback"
        contract["fallback_reason"] = reason
        return contract

    if enabled is None:
        enabled = _flag_on(os.environ.get("TEAM_OS_CTO_AGENT"))
    if not enabled:
        return _fallback("cto-agent disabled (TEAM_OS_CTO_AGENT off)")

    call = reviewer or _default_reviewer
    try:
        raw = call(build_cto_prompt(payload, cortex_verdict))
    except Exception as exc:  # noqa: BLE001 - rail failure must never break the flow
        return _fallback(f"reviewer error: {str(exc)[:160]}")

    parsed = _parse_contract(raw)
    if parsed is None:
        return _fallback("unparseable model output")

    # Assemble into the contract shape, supplying required fields + defaults.
    contract: dict[str, Any] = {
        "role": "worker",
        "source_ticket": ticket,
        "intended_behavior": parsed.get("intended_behavior", ""),
        "definition_of_done": parsed.get("definition_of_done", "") if isinstance(parsed.get("definition_of_done"), str) else "",
        "files_to_touch": parsed.get("files_to_touch", []) if isinstance(parsed.get("files_to_touch"), list) else [],
        "non_goals": parsed.get("non_goals", []),
        "assertions": parsed.get("assertions", []),
        # Real proof commands only. If the model named none, signal incompleteness
        # (so the Validator BOUNCEs) rather than papering it with a fake command.
        "commands": parsed.get("commands") if isinstance(parsed.get("commands"), list) and parsed.get("commands")
        else ["(no proof command specified — contract incomplete, Validator must BOUNCE)"],
        "bounce_conditions": parsed.get("bounce_conditions", []),
        "risk": parsed.get("risk", "medium"),
        "behavior_check_required": True,
        # fail closed: a gated ticket ALWAYS keeps the human gate on
        "human_gate_required": True if gated else bool(parsed.get("human_gate_required", False)),
        "scope_summary": parsed.get("scope_summary", ""),
    }
    errors = check_contract(contract)
    if errors:
        return _fallback(f"invalid contract: {errors[:3]}")
    contract["contract_source"] = "cto-agent"
    return contract


def _flag_on(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}
