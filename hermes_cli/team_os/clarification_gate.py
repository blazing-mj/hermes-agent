"""Early clarification gate for under-specified Team OS cards.

Ambiguous cards must stop in a Blocked/clarification lane before Worker
dispatch. The gate is deterministic and returns structured asks so MJ can answer
without reading implementation detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_AMBIGUOUS_RE = re.compile(r"\b(?:tbd|unclear|not sure|maybe|possibly|might|could be|etc|todo)\b", re.IGNORECASE)
_SCOPE_RE = re.compile(r"\b(?:which|what|where|who|when|how)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ClarificationDecision:
    needs_clarification: bool
    reasons: tuple[str, ...]
    asks: tuple[str, ...]
    lane: str = "blocked"


def assess_clarification_need(payload: dict[str, Any], contract: dict[str, Any] | None = None) -> ClarificationDecision:
    title = str(payload.get("title") or payload.get("source_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    labels = {str(label).lower() for label in payload.get("labels", []) if isinstance(label, str)}
    contract = contract or {}

    reasons: list[str] = []
    asks: list[str] = []
    if not title:
        reasons.append("title missing")
        asks.append("What outcome should this card produce?")
    if not body:
        reasons.append("body missing")
        asks.append("Add a one-paragraph scope/body before Worker dispatch.")
    elif len(body) < 40:
        reasons.append("body too sparse")
        asks.append("Expand the card body with desired behavior, non-goals, and proof expectations.")
    markers = _AMBIGUOUS_RE.findall(body)
    if markers:
        reasons.append(f"ambiguous markers: {sorted(set(m.lower() for m in markers))}")
        asks.append("Resolve ambiguous wording (TBD/maybe/unclear/etc.) before Worker dispatch.")
    if payload.get("classifier_uncertain") or "classifier:uncertain" in labels:
        reasons.append("classifier uncertain")
        asks.append("Confirm whether this should proceed as implementation, clarification, or human decision.")
    if str(payload.get("gate") or "").lower() == "clarification" or "gate:clarification" in labels:
        reasons.append("clarification gate requested")
        asks.append("Answer the card's open question(s), then remove the clarification gate label.")
    if contract.get("files_to_touch") == [] and _SCOPE_RE.search(body) and "?" in body:
        reasons.append("open question with no implementation surface")
        asks.append("Name the intended file/surface or explicitly mark this as research/planning only.")

    # De-duplicate while preserving order.
    deduped: list[str] = []
    for ask in asks:
        if ask not in deduped:
            deduped.append(ask)
    return ClarificationDecision(bool(reasons), tuple(reasons), tuple(deduped))


def build_clarification_card(source_ticket: str, decision: ClarificationDecision) -> str:
    asks = "\n".join(f"- {ask}" for ask in decision.asks) or "- Clarify the intended outcome and proof."
    reasons = "; ".join(decision.reasons) or "under-specified card"
    return (
        f"Blocked before Worker dispatch for {source_ticket}: structured clarification needed.\n\n"
        f"Reasons: {reasons}\n\n"
        f"Asks:\n{asks}\n\n"
        "No Worker was started; answer/update the card, then rerun the Cortex intake."
    )
