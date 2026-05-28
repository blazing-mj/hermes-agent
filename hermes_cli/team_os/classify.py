"""Dry-run Team OS classifier.

This module is intentionally deterministic and conservative.  It does not make
routing or execution decisions; it only labels observations so humans can audit
whether the Phase 0 taxonomy remains useful.
"""

from __future__ import annotations

from .schema import Bucket, Classification, ClassifiedObservation, MechanismType, Observation

_AMBIGUOUS_SOURCE_IDS = {"AGENTS-9", "AGENTS-10"}


def _text(observation: Observation) -> str:
    parts = [
        observation.source_id,
        observation.title,
        observation.body or "",
        observation.status or "",
        observation.project or "",
        " ".join(observation.labels),
    ]
    return " ".join(parts).casefold()


def _source_proof(observation: Observation) -> str:
    return observation.source_id or observation.url or observation.source


def classify_observation(observation: Observation) -> ClassifiedObservation:
    """Return a dry-run classification for a read-only observation."""

    text = _text(observation)
    primary = Bucket.VERIFIER
    secondary: list[Bucket] = []
    mechanism = MechanismType.VERIFIER
    confidence = "medium"
    reason = "default verifier classification"

    if any(token in text for token in ["watchdog", "alert", "health", "background_process_notifications", "raw telegram", "spam", "storage growth", "disk"]):
        primary = Bucket.OBSERVABILITY
        secondary = [Bucket.VERIFIER]
        mechanism = MechanismType.REPORT_SANITIZER
        reason = "monitoring/reporting signal needs readback or sanitization"
    elif any(token in text for token in ["klaviyo", "invalid field", "recipients_received", "conversion_value", "skill"]):
        primary = Bucket.SKILL
        secondary = [Bucket.VERIFIER]
        mechanism = MechanismType.SKILL_PATCH
        reason = "procedure/API shape needs skill or brief update"
    elif any(token in text for token in ["slack", "manifest", "mpim", "provider", "profile", "routing", "gateway"]):
        primary = Bucket.ROUTING
        secondary = [Bucket.VERIFIER]
        mechanism = MechanismType.ROUTER
        reason = "routing/channel/provider path needs explicit verification"
    elif any(token in text for token in ["memory", "recall", "session_search", "semantic", "compaction", "retrieval"]):
        primary = Bucket.RETRIEVAL
        secondary = [Bucket.SKILL]
        mechanism = MechanismType.RETRIEVAL_EVAL
        reason = "context existed but retrieval/recall quality is the suspected failure surface"
    elif any(token in text for token in ["linear", "kanban", "done without proof", "proof", "worktree", "dirty git"]):
        primary = Bucket.LINEAR
        secondary = [Bucket.VERIFIER]
        mechanism = MechanismType.LINEAR_BRIDGE
        reason = "system-of-record or worktree hygiene needs proof bridge"
    elif any(token in text for token in ["no-op", "claimed", "did not act", "talked"]):
        primary = Bucket.NO_OP
        secondary = []
        mechanism = MechanismType.PREFLIGHT
        confidence = "low"
        reason = "possible talk-without-action issue; v1 coverage remains weak"

    ambiguous = observation.source_id in _AMBIGUOUS_SOURCE_IDS
    use_as_proof = not ambiguous
    if ambiguous:
        confidence = "low"
        reason = f"{reason}; {observation.source_id} remains open/ambiguous and must not be used as proof"

    classification = Classification(
        primary_bucket=primary,
        secondary_buckets=secondary,
        mechanism_type=mechanism,
        confidence=confidence,
        source_proof=_source_proof(observation),
    )
    return ClassifiedObservation(
        observation=observation,
        classification=classification,
        dry_run=True,
        ambiguous=ambiguous,
        use_as_proof=use_as_proof,
        reason=reason,
    )
