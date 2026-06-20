"""cortex_agent.py — the REAL Cortex triage brain (Stage A of WIRE-THE-REAL-ROAD).

Today the live intake doorbell runs a keyword classifier (`_is_gated`) wearing
Cortex's name: no reasoning, no audit, no questions. Cortex is actually a real
agent (profiles/cortex/SOUL.md). This module lets the intake motor call the
REAL Cortex — an LLM that, for one ticket:

  • AUDITS it: is this real / worth doing / already done / a duplicate?
  • CLASSIFIES with reasoning: system, severity, root cause, confidence.
  • DECIDES: safe (reversible, may auto-run) vs gated (needs MJ) vs
    needs-question (too vague to proceed).
  • ASKS clarifying questions when uncertain (the loop missing today).
  • Emits a short grounding summary to hand to CTO.

Safety / rollout:
  • OFF by default. Only used when ``TEAM_OS_CORTEX_AGENT`` is truthy AND a
    reviewer is wired. The live flow keeps using the keyword classifier until
    the operator flips this on at turn-on.
  • FAIL-SAFE: any error (rail down, bad JSON, timeout) falls back to the
    keyword verdict, so the intake flow can never break because of this.
  • This module performs NO board writes and NO side effects — it only returns
    a verdict dict. The caller decides what to do with it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

_CLAUDE_MAX_CODE = os.environ.get(
    "CLAUDE_MAX_CODE", str(Path.home() / ".hermes" / "bin" / "claude-max-code")
)

# Reviewer signature: (prompt: str) -> str (raw model text). Injectable for tests.
Reviewer = Callable[[str], str]

_CORTEX_SYSTEM = (
    "You are Cortex, MJ's system-ops triage brain for Hermes/OpenClaw Team OS. "
    "You receive ONE Linear ticket and must triage it before any work starts. "
    "You do not implement anything; you audit, classify, and decide routing.\n\n"
    "Decide, with reasoning:\n"
    "1. AUDIT — is the ticket real and worth doing now? Is it already done, a "
    "duplicate, or stale? If it's not worth doing, say so.\n"
    "2. CLASSIFY — target system (HERMES or OPENCLAW), severity (low|medium|high), "
    "the suspected root need/cause, and your confidence 0..1.\n"
    "3. DECIDE one of:\n"
    "   - 'safe'         → reversible work (code/tests/docs/config) touching NO "
    "money, credentials, live sends, production, customer, or trading surfaces; "
    "may proceed without MJ.\n"
    "   - 'gated'        → touches a denied/consequential surface OR is "
    "irreversible OR you are not confident → must go to MJ.\n"
    "   - 'needs-question' → too vague/underspecified to proceed; you must ask.\n"
    "4. QUESTIONS — if needs-question (or you'd proceed but have real doubts), "
    "list the specific questions MJ must answer. Empty list otherwise.\n"
    "5. GROUNDING — one or two sentences summarizing the root need, to hand to CTO.\n\n"
    "FAIL CLOSED: if uncertain whether something is consequential, choose 'gated'. "
    "Use NO tools. Do not investigate the codebase. Decide ONLY from the ticket "
    "text below and respond with the JSON immediately — if the ticket is too "
    "vague to decide, that is exactly when you return decision='needs-question' "
    "with the questions you need answered.\n"
    "Output ONLY a single JSON object, no prose, with exactly these keys:\n"
    '{"is_real": bool, "worth_doing": bool, "already_done_or_duplicate": bool, '
    '"system": "HERMES|OPENCLAW", "severity": "low|medium|high", '
    '"root_cause": str, "confidence": number, '
    '"decision": "safe|gated|needs-question", "gated_reason": str, '
    '"questions": [str], "grounding_summary": str, '
    '"recommended_route": str, "reason": str}'
)


def build_cortex_prompt(payload: dict[str, Any]) -> str:
    """Render the triage prompt for one ticket payload."""
    labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    ticket = str(payload.get("identifier") or payload.get("id") or "?")
    return "\n".join([
        _CORTEX_SYSTEM,
        "",
        f"TICKET: {ticket}",
        f"TITLE: {payload.get('title') or '(none)'}",
        f"LABELS: {', '.join(str(x) for x in labels) or '(none)'}",
        f"PROJECT: {payload.get('project') or '(none)'}",
        "DESCRIPTION:",
        str(payload.get("body") or payload.get("description") or "(empty)"),
        "",
        "Return the JSON verdict now.",
    ])


def _default_reviewer(prompt: str, *, timeout: float = 120.0) -> str:
    """Invoke the real Claude rail (same pattern as worker/validator runners)."""
    result = subprocess.run(
        (
            _CLAUDE_MAX_CODE, "team-os-cortex-agent", "--",
            # max-turns 2 (not 1): if the model attempts a tool call despite the
            # no-tools instruction, the denied call burns a turn — the extra
            # turn lets it still return the JSON instead of error_max_turns.
            "-p", prompt, "--model", "opus", "--max-turns", "2",
            "--disallowedTools",
            "Bash,Read,Edit,Write,Glob,Grep,LS,Task,TodoWrite,WebFetch,WebSearch",
        ),
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    return (result.stdout or "").strip()


_VALID_DECISIONS = {"safe", "gated", "needs-question"}


def _parse_verdict(raw: str) -> Optional[dict[str, Any]]:
    """Extract + validate the JSON verdict. Returns None if unusable."""
    text = (raw or "").strip()
    # tolerate ```json fences and a {"result": "..."} wrapper
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
        return _parse_verdict(obj["result"])
    if not isinstance(obj, dict):
        return None
    if obj.get("decision") not in _VALID_DECISIONS:
        return None
    obj.setdefault("questions", [])
    if not isinstance(obj["questions"], list):
        obj["questions"] = [str(obj["questions"])]
    return obj


def cortex_audit(
    payload: dict[str, Any],
    *,
    keyword_gated: bool,
    reviewer: Reviewer | None = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    """Real Cortex triage for one ticket, with keyword fail-safe.

    Args:
        payload: the Linear ticket payload (title/body/labels/project/id).
        keyword_gated: the deterministic keyword verdict, used as the fallback
            and as a cross-check signal.
        reviewer: injectable model caller (defaults to the Claude rail). Tests
            pass a stub.
        enabled: override the TEAM_OS_CORTEX_AGENT flag (for tests).

    Returns a verdict dict ALWAYS containing at least:
        {"gated": bool, "decision": str, "questions": [...], "source": str}
    where source is "cortex-agent" on a real audit or "keyword-fallback" when
    the agent is off or errored. Never raises.
    """
    def _fallback(reason: str) -> dict[str, Any]:
        return {
            "gated": bool(keyword_gated),
            "decision": "gated" if keyword_gated else "safe",
            "questions": [],
            "source": "keyword-fallback",
            "fallback_reason": reason,
        }

    if enabled is None:
        enabled = _flag_on(os.environ.get("TEAM_OS_CORTEX_AGENT"))
    if not enabled:
        return _fallback("cortex-agent disabled (TEAM_OS_CORTEX_AGENT off)")

    call = reviewer or _default_reviewer
    try:
        raw = call(build_cortex_prompt(payload))
    except Exception as exc:  # noqa: BLE001 - rail failure must never break intake
        return _fallback(f"reviewer error: {str(exc)[:160]}")

    verdict = _parse_verdict(raw)
    if verdict is None:
        return _fallback("unparseable model output")

    decision = verdict["decision"]
    # needs-question and gated both stop auto-flow; only 'safe' is non-gated.
    gated = decision in ("gated", "needs-question")
    # Safety cross-check: if the cheap keyword classifier says gated but the
    # agent said safe, trust the stricter signal (fail closed).
    if keyword_gated and not gated:
        gated = True
        verdict["safety_override"] = "keyword classifier flagged a gated surface the agent missed"
    verdict["gated"] = gated
    verdict["source"] = "cortex-agent"
    return verdict


def _flag_on(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}
