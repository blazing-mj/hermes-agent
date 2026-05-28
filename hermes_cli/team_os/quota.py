"""Quota guard Phase 1 stub.

Phase 1 is deliberately not a quota guard.  It only exposes an explicit
`unknown` status so downstream reporting cannot pretend to know subscription
utilization before provider-specific probes are proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuotaStatus:
    provider: str
    availability: str
    confidence: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "availability": self.availability,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def quota_status_unknown(provider: str) -> QuotaStatus:
    """Return the only allowed Phase 1 quota verdict."""

    return QuotaStatus(
        provider=provider,
        availability="unknown",
        confidence="unknown",
        reason="Phase 1 has no trusted quota probe yet; exact utilization is intentionally not reported.",
    )
