"""Task router v1 for Team OS Phase 5.

Decides between the Codex and Claude Code Max subscriptions for a single
task in dry-run mode.  The router never spawns or mutates anything; it only
returns a structured ``RouteDecision`` containing the chosen dispatcher and
a logged explanation.

Policy summary (subscription-only; no API fallback):
    * Heavy implementation / review tasks always prefer ``claude-max`` and
      refuse Codex even when Codex quota looks generous.  Codex usage is
      not confirmed for these shapes.
    * Host / direct-chat shaped tasks may route to ``codex`` only when a
      caller-supplied probe confirms availability AND Codex quota
      confidence is high or medium.
    * Quota confidence below "high"/"medium" blocks the dispatcher.
    * If no subscription is safe, the dispatcher is ``"none"`` and the
      task requires human dispatch.  There is no automatic API fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

_HEAVY_TASK_TYPES = {
    "code",
    "implementation",
    "build",
    "refactor",
    "review",
    "test",
    "fix",
    "bug",
}
_HEAVY_LABEL_TOKENS = {
    "type:code",
    "type:implementation",
    "type:build",
    "type:refactor",
    "type:review",
    "type:test",
    "type:fix",
    "type:bug",
}
_HOST_TASK_TYPES = {"host", "chat", "direct-chat", "host-chat"}
_HOST_LABEL_TOKENS = {
    "type:host",
    "type:chat",
    "type:direct-chat",
    "scope:host",
}
_HIGH_CONFIDENCE = {"high", "medium"}
_NO_API_FALLBACK_PHRASE = "no automatic API fallback"


@dataclass(frozen=True)
class TaskHints:
    """Routing-relevant hints derived from a task without mutating it."""

    task_id: str
    labels: tuple[str, ...] = ()
    task_type: str = "unknown"
    quota_confidence_codex: str = "unknown"
    quota_confidence_claude_max: str = "unknown"

    def __post_init__(self) -> None:
        # Normalize labels to a tuple so equality/repr stay stable even when
        # callers hand in lists.
        object.__setattr__(self, "labels", tuple(self.labels))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "labels": list(self.labels),
            "task_type": self.task_type,
            "quota_confidence_codex": self.quota_confidence_codex,
            "quota_confidence_claude_max": self.quota_confidence_claude_max,
        }


@dataclass(frozen=True)
class RouteDecision:
    """Structured router output suitable for JSON logging."""

    task_id: str
    dispatcher: str
    reason: str
    explanation: str
    considered: dict[str, str] = field(default_factory=dict)
    requires_human_dispatch: bool = False
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "dispatcher": self.dispatcher,
            "reason": self.reason,
            "explanation": self.explanation,
            "considered": dict(self.considered),
            "requires_human_dispatch": self.requires_human_dispatch,
            "dry_run": self.dry_run,
        }


def _is_heavy(hints: TaskHints) -> bool:
    if hints.task_type.lower() in _HEAVY_TASK_TYPES:
        return True
    return any(label.lower() in _HEAVY_LABEL_TOKENS for label in hints.labels)


def _is_host_shape(hints: TaskHints) -> bool:
    if hints.task_type.lower() in _HOST_TASK_TYPES:
        return True
    return any(label.lower() in _HOST_LABEL_TOKENS for label in hints.labels)


def _probe_codex(codex_probe: Callable[[], bool] | None) -> tuple[bool, str]:
    """Run the caller-supplied Codex availability probe safely.

    Returns ``(available, status_message)`` where ``status_message`` is the
    human-readable explanation logged in ``considered``.  A missing probe is
    treated as unconfirmed because Codex usage is explicitly not assumed.
    """

    if codex_probe is None:
        return False, "blocked: codex probe not supplied (codex unconfirmed)"
    try:
        ok = bool(codex_probe())
    except Exception as exc:  # noqa: BLE001 — probe failure must flip availability
        return False, f"blocked: codex probe raised ({exc.__class__.__name__})"
    if not ok:
        return False, "blocked: codex probe returned unavailable"
    return True, "available"


def route_task(
    hints: TaskHints,
    *,
    codex_probe: Callable[[], bool] | None = None,
) -> RouteDecision:
    """Route a single task to ``codex`` or ``claude-max`` (or ``none``).

    The router is deterministic, dry-run, and never reaches the network.
    The ``codex_probe`` is the only injection point for runtime availability.
    """

    considered: dict[str, str] = {}

    codex_available, codex_status = _probe_codex(codex_probe)
    considered["codex"] = codex_status

    claude_quota_ok = hints.quota_confidence_claude_max in _HIGH_CONFIDENCE
    if claude_quota_ok:
        considered["claude-max"] = (
            f"available (quota confidence {hints.quota_confidence_claude_max})"
        )
    else:
        considered["claude-max"] = (
            f"blocked: quota confidence {hints.quota_confidence_claude_max}"
        )

    codex_quota_ok = hints.quota_confidence_codex in _HIGH_CONFIDENCE
    if codex_available and not codex_quota_ok:
        considered["codex"] = (
            f"blocked: quota confidence {hints.quota_confidence_codex}"
        )

    heavy = _is_heavy(hints)
    host_shape = _is_host_shape(hints)

    if heavy:
        if claude_quota_ok:
            reason = "heavy implementation/review task — prefer claude-max"
            explanation = (
                f"task {hints.task_id} type={hints.task_type} labels={list(hints.labels)} "
                "is heavy implementation/review; subscription policy routes it to "
                "claude-max. Codex is not used for heavy code shapes even if its "
                f"quota looks generous. {_NO_API_FALLBACK_PHRASE}."
            )
            return RouteDecision(
                task_id=hints.task_id,
                dispatcher="claude-max",
                reason=reason,
                explanation=explanation,
                considered=considered,
                requires_human_dispatch=False,
            )
        reason = (
            f"{_NO_API_FALLBACK_PHRASE}: claude-max quota confidence "
            f"{hints.quota_confidence_claude_max}; codex refused for heavy task"
        )
        explanation = (
            f"task {hints.task_id} is heavy implementation/review but claude-max "
            f"quota confidence is {hints.quota_confidence_claude_max}. Codex is "
            f"refused for heavy shapes by policy. {_NO_API_FALLBACK_PHRASE}; this "
            "task requires human dispatch."
        )
        return RouteDecision(
            task_id=hints.task_id,
            dispatcher="none",
            reason=reason,
            explanation=explanation,
            considered=considered,
            requires_human_dispatch=True,
        )

    if host_shape:
        if codex_available and codex_quota_ok:
            label_hint = _first_host_label(hints) or hints.task_type
            reason = f"host/direct-chat task ({label_hint}) — codex eligible"
            explanation = (
                f"task {hints.task_id} is host/direct-chat shaped and the codex "
                "probe confirmed availability with sufficient quota confidence. "
                f"{_NO_API_FALLBACK_PHRASE} is needed because codex is the "
                "preferred subscription for direct-chat work."
            )
            return RouteDecision(
                task_id=hints.task_id,
                dispatcher="codex",
                reason=reason,
                explanation=explanation,
                considered=considered,
                requires_human_dispatch=False,
            )
        if claude_quota_ok:
            reason = (
                "host task but codex unavailable; falling back to claude-max "
                "subscription (no API fallback)"
            )
            explanation = (
                f"task {hints.task_id} is host shaped but codex is "
                f"{considered['codex']}. Claude-max is used because its "
                f"subscription quota confidence is "
                f"{hints.quota_confidence_claude_max}. {_NO_API_FALLBACK_PHRASE}."
            )
            return RouteDecision(
                task_id=hints.task_id,
                dispatcher="claude-max",
                reason=reason,
                explanation=explanation,
                considered=considered,
                requires_human_dispatch=False,
            )
        reason = (
            f"host task but codex {considered['codex']}; claude-max quota "
            f"confidence {hints.quota_confidence_claude_max}"
        )
        explanation = (
            f"task {hints.task_id} is host shaped, codex is "
            f"{considered['codex']}, and claude-max quota confidence is "
            f"{hints.quota_confidence_claude_max}. {_NO_API_FALLBACK_PHRASE}; "
            "this task requires human dispatch."
        )
        return RouteDecision(
            task_id=hints.task_id,
            dispatcher="none",
            reason=reason,
            explanation=explanation,
            considered=considered,
            requires_human_dispatch=True,
        )

    # Unknown shape — be conservative: only route to claude-max if quota is
    # confidently available; otherwise require a human to decide.
    if claude_quota_ok:
        reason = "unknown task shape — defaulting to claude-max subscription"
        explanation = (
            f"task {hints.task_id} has no recognized type/labels for routing. "
            "Defaulting to claude-max because its subscription quota confidence "
            f"is {hints.quota_confidence_claude_max}. {_NO_API_FALLBACK_PHRASE}."
        )
        return RouteDecision(
            task_id=hints.task_id,
            dispatcher="claude-max",
            reason=reason,
            explanation=explanation,
            considered=considered,
            requires_human_dispatch=False,
        )
    reason = (
        f"unknown task shape and quota confidence "
        f"claude-max={hints.quota_confidence_claude_max} "
        f"codex={hints.quota_confidence_codex}"
    )
    explanation = (
        f"task {hints.task_id} has no recognized type/labels and no subscription "
        f"has high/medium quota confidence. {_NO_API_FALLBACK_PHRASE}; this task "
        "requires human dispatch."
    )
    return RouteDecision(
        task_id=hints.task_id,
        dispatcher="none",
        reason=reason,
        explanation=explanation,
        considered=considered,
        requires_human_dispatch=True,
    )


def _first_host_label(hints: TaskHints) -> str | None:
    for label in hints.labels:
        if label.lower() in _HOST_LABEL_TOKENS:
            return label
    return None
