"""Event router for Team OS — AGENTS-179 Stage 4.

Queues Linear observations into the durable outbox with per-source-id dedupe.
No network calls, no Linear API, no side-effects beyond writing to the local DB.

Usage::

    from hermes_cli.team_os.event_router import route_linear_observation

    queued = route_linear_observation(obs, state)
    if queued is None:
        # already processed — skip
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .approvals import ReversibilityCategory
from .failure_cost import assess_failure_cost

if TYPE_CHECKING:
    from hermes_cli.team_os.db import TeamOSState
    from hermes_cli.team_os.schema import Observation

_LINEAR_EVENT_TYPE = "linear_observation"


@dataclass(frozen=True)
class QueuedEvent:
    """A single outbox event returned by the router."""

    event_id: int
    event_type: str
    source_id: str
    source: str
    payload: dict[str, Any]
    created_at: int
    status: str = "pending"


def _failure_category_for_observation(obs: "Observation") -> ReversibilityCategory:
    text = " ".join([obs.title or "", obs.body or "", " ".join(obs.labels)]).lower()
    if any(token in text for token in ("credential", "token", "secret", "keyring", "pat")):
        return ReversibilityCategory.CREDENTIAL_CHANGE
    if any(token in text for token in ("delete", "drop", "purge", "mass-delete")):
        return ReversibilityCategory.MASS_DELETE
    if any(token in text for token in ("migration", "migrate", "schema")):
        return ReversibilityCategory.DATA_MIGRATION
    if any(token in text for token in ("external", "client", "production", "send", "dispatch")):
        return ReversibilityCategory.EXTERNAL_SIDE_EFFECT
    if any(
        token in text
        for token in (
            "failure-cost:low",
            "low-failure-cost",
            "planner polish",
            "acceptance criteria",
            "docs-only",
            "polish",
        )
    ):
        return ReversibilityCategory.FULL_INSTANT
    return ReversibilityCategory.FULL_EFFORT


def route_linear_observation(
    obs: "Observation",
    state: "TeamOSState",
) -> QueuedEvent | None:
    """Queue a Linear observation in the durable outbox.

    Deduplicates by source_id — calling this twice for the same observation
    returns the same QueuedEvent both times, unless it has already been
    processed (in which case it returns None).

    Args:
        obs: The observation to route.
        state: The TeamOSState instance to use for persistence.

    Returns:
        A :class:`QueuedEvent` for a new or existing pending event, or
        ``None`` if the event for this source_id was already processed.
    """
    failure_cost = assess_failure_cost(_failure_category_for_observation(obs))
    payload: dict[str, Any] = {
        "source": obs.source,
        "source_id": obs.source_id,
        "title": obs.title,
        "body": obs.body,
        "status": obs.status,
        "project": obs.project,
        "labels": list(obs.labels),
        "url": obs.url,
        "failure_cost_tier": failure_cost.tier,
        "requires_mj_review": failure_cost.requires_mj_review,
        "failure_cost_reason": failure_cost.reason,
    }

    event_id = state.queue_for_dispatch(
        event_type=_LINEAR_EVENT_TYPE,
        source_id=obs.source_id,
        source=obs.source,
        payload=payload,
    )

    row = state.get_outbox_event(event_id)
    if row["state"] in {"dispatching", "succeeded", "failed", "abandoned", "mj_review"}:
        return None
    if row["payload"].get("requires_mj_review"):
        state.mark_event_mj_review(event_id, reason=row["payload"]["failure_cost_reason"])
        return None

    return QueuedEvent(
        event_id=event_id,
        event_type=row["event_type"],
        source_id=row["source_id"],
        source=row["source"],
        payload=row["payload"],
        created_at=row["created_at"],
        status=row["status"],
    )
