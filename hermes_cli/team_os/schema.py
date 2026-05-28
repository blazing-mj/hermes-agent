"""Schema primitives for the Team OS read-only rollout."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Bucket(str, Enum):
    VERIFIER = "verifier"
    HOOK = "hook"
    SKILL = "skill"
    ROUTING = "routing"
    RETRIEVAL = "retrieval"
    LINEAR = "Linear"
    NO_OP = "no-op"
    OBSERVABILITY = "observability"


class MechanismType(str, Enum):
    VERIFIER = "verifier"
    PREFLIGHT = "preflight"
    ROUTER = "router"
    RETRIEVAL_EVAL = "retrieval_eval"
    SKILL_PATCH = "skill_patch"
    LINEAR_BRIDGE = "linear_bridge"
    REPORT_SANITIZER = "report_sanitizer"
    WORKTREE_HYGIENE = "worktree_hygiene"
    QUOTA_PROBE = "quota_probe"
    UNKNOWN = "unknown"


VALID_CONFIDENCE = {"high", "medium", "low", "unknown"}


@dataclass(frozen=True)
class Observation:
    """Read-only observation from Linear, Kanban, or another source."""

    source: str
    source_id: str
    title: str
    body: str | None = None
    status: str | None = None
    project: str | None = None
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    collected_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "project": self.project,
            "labels": list(self.labels),
            "url": self.url,
            "collected_at": self.collected_at,
        }


@dataclass(frozen=True)
class Classification:
    """Approved Phase 1 classification schema."""

    primary_bucket: Bucket
    secondary_buckets: list[Bucket]
    mechanism_type: MechanismType
    confidence: str
    source_proof: str

    def __post_init__(self) -> None:
        if self.confidence not in VALID_CONFIDENCE:
            raise ValueError(f"confidence must be one of {sorted(VALID_CONFIDENCE)}")
        if not self.source_proof:
            raise ValueError("source_proof is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_bucket": self.primary_bucket.value,
            "secondary_buckets": [bucket.value for bucket in self.secondary_buckets],
            "mechanism_type": self.mechanism_type.value,
            "confidence": self.confidence,
            "source_proof": self.source_proof,
        }


@dataclass(frozen=True)
class ClassifiedObservation:
    observation: Observation
    classification: Classification
    dry_run: bool = True
    ambiguous: bool = False
    use_as_proof: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_dict(),
            "classification": self.classification.to_dict(),
            "dry_run": self.dry_run,
            "ambiguous": self.ambiguous,
            "use_as_proof": self.use_as_proof,
            "reason": self.reason,
        }
