"""Gated Cortex orchestration for Team OS — AGENTS-179 Stage 4.

This module is intentionally deterministic and disabled-by-default.  It may poll
Linear through an injected collector, queue observations into the durable outbox,
and reconcile in-flight rows on restart.  It does not run live autonomous
dispatch unless the caller explicitly opts into ``active=True`` and
``dry_run=False`` *and* the gateway health probe passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Any

from .event_router import QueuedEvent, route_linear_observation

if TYPE_CHECKING:
    from .db import TeamOSState
    from .schema import Observation

CORTEX_LAUNCHD_LABEL = "ai.hermes.team-os-cortex"

CORTEX_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hermes.team-os-cortex</string>
    <key>ProgramArguments</key>
    <array>
        <string>{hermes_bin}</string>
        <string>team-os</string>
        <string>cortex</string>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
</dict>
</plist>"""


@dataclass(frozen=True)
class CortexConfig:
    """Configuration for one Cortex orchestration cycle."""

    active: bool = False
    dry_run: bool = True
    max_dispatch_per_cycle: int = 1


@dataclass(frozen=True)
class CortexResult:
    """Result of a single gated orchestration cycle."""

    reconciled: tuple[dict[str, Any], ...]
    queued: tuple[QueuedEvent, ...]
    skipped: tuple[str, ...]
    dispatched: int
    dry_run: bool
    active: bool
    paused_reason: str | None = None

    @property
    def reconcile_count(self) -> int:
        return len(self.reconciled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": list(self.reconciled),
            "reconcile_count": self.reconcile_count,
            "queued": [event.__dict__ for event in self.queued],
            "skipped": list(self.skipped),
            "dispatched": self.dispatched,
            "dry_run": self.dry_run,
            "active": self.active,
            "paused_reason": self.paused_reason,
        }


def run_cortex(
    state: "TeamOSState",
    config: CortexConfig | None = None,
    *,
    observations: Iterable["Observation"] = (),
    collector: Callable[[], Iterable["Observation"]] | None = None,
    gateway_health_probe: Callable[[], Any] | None = None,
    dispatch: Callable[[dict[str, Any]], object] | None = None,
) -> CortexResult:
    """Run one poll/reconcile/dispatch-gate cycle.

    Order is load-bearing: reconcile first, then poll/queue, then maybe dispatch.
    Dry-run/default mode never calls ``dispatch``.  Active mode is fail-closed
    if no gateway health probe is supplied or if the probe is unhealthy.
    """
    cfg = config or CortexConfig()
    reconciled = tuple(state.reconcile_in_flight(reason="cortex reconcile-on-restart"))

    observed = list(observations)
    if collector is not None:
        observed.extend(list(collector()))

    queued: list[QueuedEvent] = []
    skipped: list[str] = []
    for obs in observed:
        event = route_linear_observation(obs, state)
        if event is None:
            skipped.append(obs.source_id)
        else:
            queued.append(event)

    paused_reason: str | None = None
    dispatched = 0
    if not cfg.active or cfg.dry_run:
        paused_reason = "dry-run or inactive — live dispatch disabled"
    else:
        if gateway_health_probe is None:
            paused_reason = "gateway health probe missing — dispatch paused"
        else:
            health = gateway_health_probe()
            healthy = bool(getattr(health, "healthy", health))
            if not healthy:
                paused_reason = getattr(health, "message", "gateway/runtime unhealthy")
            elif dispatch is None:
                paused_reason = "dispatch function missing — dispatch paused"
            else:
                for event in state.list_outbox_events(states=("queued",))[: cfg.max_dispatch_per_cycle]:
                    state.mark_event_dispatching(int(event["id"]))
                    dispatching_event = state.get_outbox_event(int(event["id"]))
                    try:
                        dispatch(dispatching_event)
                    except Exception as exc:  # pragma: no cover - defensive path
                        state.mark_event_failed(int(event["id"]), reason=str(exc))
                        raise
                    else:
                        state.mark_event_succeeded(int(event["id"]))
                        dispatched += 1

    return CortexResult(
        reconciled=reconciled,
        queued=tuple(queued),
        skipped=tuple(skipped),
        dispatched=dispatched,
        dry_run=cfg.dry_run,
        active=cfg.active,
        paused_reason=paused_reason,
    )
