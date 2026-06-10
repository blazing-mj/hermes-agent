"""Full-backlog intake reconciliation for event-driven Team OS Cortex.

Linear webhooks are only doorbells: they wake this reconciler, but never define
which card should run. Every wake source (doorbell, completion, sweep) performs
the same full Backlog scan against the durable intake ledger, then the picker
selects at most one card under the existing single-card lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class WakeSource(str, Enum):
    DOORBELL = "doorbell"
    COMPLETION = "completion"
    SWEEP = "sweep"


@dataclass(frozen=True)
class BacklogCard:
    id: str
    headline: str
    priority: str | int | None
    age: int
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReconcileResult:
    wake_source: WakeSource
    added: tuple[str, ...]
    removed: tuple[str, ...]
    current_count: int
    recheck_requested: bool = False


@dataclass(frozen=True)
class PickResult:
    card: dict[str, Any] | None
    busy: bool
    recheck_requested: bool


def _coerce_card(raw: BacklogCard | dict[str, Any]) -> BacklogCard:
    if isinstance(raw, BacklogCard):
        return raw
    return BacklogCard(
        id=str(raw["id"]),
        headline=str(raw.get("headline") or raw.get("title") or raw["id"]),
        priority=raw.get("priority"),
        age=int(raw.get("age", 0)),
        payload=dict(raw.get("payload") or raw),
    )


def priority_sort_key(priority: str | int | None) -> tuple[int, str]:
    """Sort key where Urgent jumps the queue and lower numeric priority wins."""

    if priority is None:
        return (99, "")
    if isinstance(priority, int):
        return (priority, str(priority))
    text = str(priority).strip().lower()
    if text == "urgent":
        return (0, text)
    numeric = {"high": 1, "medium": 2, "normal": 3, "low": 4, "none": 99}
    if text in numeric:
        return (numeric[text], text)
    try:
        return (int(text), text)
    except ValueError:
        return (50, text)


def sort_candidates(cards: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority first, Urgent first, oldest within priority."""

    return sorted(cards, key=lambda card: (priority_sort_key(card.get("priority")), -int(card.get("age", 0)), str(card.get("id", ""))))


def reconcile_full_backlog(*, state: Any, backlog_cards: Iterable[BacklogCard | dict[str, Any]], wake_source: WakeSource | str) -> ReconcileResult:
    """Run the single source-of-truth reconcile for any wake source."""

    source = wake_source if isinstance(wake_source, WakeSource) else WakeSource(str(wake_source))
    cards = [_coerce_card(card) for card in backlog_cards]
    delta = state.reconcile_intake_ledger(cards)
    return ReconcileResult(
        wake_source=source,
        added=tuple(delta["added"]),
        removed=tuple(delta["removed"]),
        current_count=int(delta["current_count"]),
        recheck_requested=bool(delta.get("recheck_requested", False)),
    )


def pick_one_after_reconcile(*, state: Any, busy: bool = False) -> PickResult:
    """Pick exactly one top card, or only request a recheck if Cortex is busy."""

    if busy:
        state.set_intake_recheck_requested(True)
        return PickResult(card=None, busy=True, recheck_requested=True)
    candidates = sort_candidates(state.list_intake_candidates())
    return PickResult(card=candidates[0] if candidates else None, busy=False, recheck_requested=False)
