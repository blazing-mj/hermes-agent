"""Local SQLite state for Team OS Phase 1 snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .approvals import ApprovalStatus, ReversibilityCategory, decision_to_status
from .schema import ClassifiedObservation


class TeamOSState:
    """Small local DB for read-only source snapshots and dry-run labels."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT,
                    project TEXT,
                    labels_json TEXT NOT NULL,
                    url TEXT,
                    primary_bucket TEXT NOT NULL,
                    secondary_buckets_json TEXT NOT NULL,
                    mechanism_type TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    source_proof TEXT NOT NULL,
                    ambiguous INTEGER NOT NULL DEFAULT 0,
                    use_as_proof INTEGER NOT NULL DEFAULT 1,
                    reason TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reversibility_category TEXT NOT NULL,
                    reversibility_reason TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    actor TEXT,
                    decision_reason TEXT,
                    modified_scope TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_confidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    goal_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    queued_at INTEGER,
                    dispatching_at INTEGER,
                    completed_at INTEGER,
                    last_error TEXT,
                    escalation_required INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(event_type, source_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_ledger (
                    id TEXT PRIMARY KEY,
                    headline TEXT NOT NULL,
                    priority TEXT,
                    age INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def record_snapshot(self, classified: list[ClassifiedObservation]) -> int:
        self.init_schema()
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO snapshots(created_at, dry_run) VALUES (?, 1)",
                (now,),
            )
            if cur.lastrowid is None:
                raise RuntimeError("failed to create Team OS snapshot row")
            snapshot_id = int(cur.lastrowid)
            for item in classified:
                obs = item.observation
                cls = item.classification
                conn.execute(
                    """
                    INSERT INTO classifications(
                        snapshot_id, source, source_id, title, status, project,
                        labels_json, url, primary_bucket, secondary_buckets_json,
                        mechanism_type, confidence, source_proof, ambiguous,
                        use_as_proof, reason, dry_run
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        obs.source,
                        obs.source_id,
                        obs.title,
                        obs.status,
                        obs.project,
                        json.dumps(obs.labels, sort_keys=True),
                        obs.url,
                        cls.primary_bucket.value,
                        json.dumps([b.value for b in cls.secondary_buckets], sort_keys=True),
                        cls.mechanism_type.value,
                        cls.confidence,
                        cls.source_proof,
                        1 if item.ambiguous else 0,
                        1 if item.use_as_proof else 0,
                        item.reason,
                        1 if item.dry_run else 0,
                    ),
                )
            conn.commit()
        return snapshot_id

    def list_classifications(self, snapshot_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM classifications WHERE snapshot_id = ? ORDER BY id ASC",
                (snapshot_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["labels"] = json.loads(data.pop("labels_json"))
            data["secondary_buckets"] = json.loads(data.pop("secondary_buckets_json"))
            data["ambiguous"] = bool(data["ambiguous"])
            data["use_as_proof"] = bool(data["use_as_proof"])
            data["dry_run"] = bool(data["dry_run"])
            result.append(data)
        return result

    def create_approval_request(
        self,
        *,
        task_id: str,
        title: str,
        action: str,
        reversibility_category: ReversibilityCategory,
        reversibility_reason: str,
        prompt: str,
    ) -> int:
        self.init_schema()
        now = int(time.time())
        category = reversibility_category.value
        status = (
            ApprovalStatus.PENDING
            if reversibility_category
            in {
                ReversibilityCategory.DATA_MIGRATION,
                ReversibilityCategory.CREDENTIAL_CHANGE,
                ReversibilityCategory.EXTERNAL_SIDE_EFFECT,
                ReversibilityCategory.MASS_DELETE,
                ReversibilityCategory.NONE,
            }
            else ApprovalStatus.AUTO_APPROVED
        )
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO approvals(
                    created_at, updated_at, task_id, title, action,
                    reversibility_category, reversibility_reason, prompt, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    task_id,
                    title,
                    action,
                    category,
                    reversibility_reason,
                    prompt,
                    status.value,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("failed to create approval request")
            approval_id = int(cur.lastrowid)
            conn.commit()
        return approval_id

    def get_approval_request(self, approval_id: int) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"approval request not found: {approval_id}")
        return dict(row)

    def record_approval_decision(
        self,
        approval_id: int,
        *,
        decision: str,
        actor: str,
        reason: str | None = None,
        modified_scope: str | None = None,
    ) -> dict[str, Any]:
        self.init_schema()
        status = decision_to_status(decision)
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE approvals
                SET updated_at = ?, status = ?, decision = ?, actor = ?,
                    decision_reason = ?, modified_scope = ?
                WHERE id = ?
                """,
                (
                    now,
                    status.value,
                    decision,
                    actor,
                    reason,
                    modified_scope,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise KeyError(f"approval request not found: {approval_id}")
            conn.commit()
        return self.get_approval_request(approval_id)

    # -----------------------------------------------------------------------
    # Phase 8 — task confidence persistence
    # -----------------------------------------------------------------------

    def persist_task_confidence(
        self,
        *,
        goal_id: str,
        task_id: str,
        confidence: str,
        reasons: list[str],
        source: str = "decomposer",
    ) -> int:
        self.init_schema()
        now = int(time.time())
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM task_confidence WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if existing is not None:
                row_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE task_confidence
                    SET created_at = ?, goal_id = ?, confidence = ?, reasons_json = ?, source = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        goal_id,
                        confidence,
                        json.dumps(reasons, sort_keys=True),
                        source,
                        row_id,
                    ),
                )
                conn.commit()
                return row_id

            cur = conn.execute(
                """
                INSERT INTO task_confidence(created_at, goal_id, task_id, confidence, reasons_json, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, goal_id, task_id, confidence, json.dumps(reasons, sort_keys=True), source),
            )
            if cur.lastrowid is None:
                raise RuntimeError("failed to create task_confidence row")
            row_id = int(cur.lastrowid)
            conn.commit()
        return row_id

    def get_task_confidence(self, task_id: str) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_confidence WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"task_confidence not found for task_id: {task_id!r}")
        data = dict(row)
        data["reasons"] = json.loads(data.pop("reasons_json"))
        return data

    def list_task_confidence(self, goal_id: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_confidence WHERE goal_id = ? ORDER BY id ASC",
                (goal_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["reasons"] = json.loads(data.pop("reasons_json"))
            result.append(data)
        return result

    # -----------------------------------------------------------------------
    # AGENTS-223 — full-backlog intake ledger
    # -----------------------------------------------------------------------

    def reconcile_intake_ledger(self, cards: list[Any]) -> dict[str, Any]:
        """Diff a full Linear Backlog scan into the durable intake ledger.

        Cards must provide ``id``, ``headline``, ``priority``, and ``age`` as
        attributes or mapping keys. Missing cards are added, cards absent from
        the latest full Backlog scan are removed, and existing rows are updated.
        """

        self.init_schema()
        now = int(time.time())
        normalized: dict[str, dict[str, Any]] = {}
        for raw in cards:
            if isinstance(raw, dict):
                card_id = str(raw["id"])
                headline = str(raw.get("headline") or raw.get("title") or card_id)
                priority = raw.get("priority")
                age = int(raw.get("age", 0))
                payload = dict(raw.get("payload") or raw)
            else:
                card_id = str(getattr(raw, "id"))
                headline = str(getattr(raw, "headline"))
                priority = getattr(raw, "priority")
                age = int(getattr(raw, "age"))
                payload = dict(getattr(raw, "payload", None) or {"id": card_id, "headline": headline, "priority": priority, "age": age})
            normalized[card_id] = {
                "id": card_id,
                "headline": headline,
                "priority": None if priority is None else str(priority),
                "age": age,
                "payload": payload,
            }

        with self.connect() as conn:
            existing_rows = conn.execute("SELECT id FROM intake_ledger").fetchall()
            existing = {str(row["id"]) for row in existing_rows}
            incoming = set(normalized)
            added = sorted(incoming - existing)
            removed = sorted(existing - incoming)
            for card in normalized.values():
                conn.execute(
                    """
                    INSERT INTO intake_ledger(id, headline, priority, age, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        headline = excluded.headline,
                        priority = excluded.priority,
                        age = excluded.age,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        card["id"],
                        card["headline"],
                        card["priority"],
                        card["age"],
                        json.dumps(card["payload"], sort_keys=True),
                        now,
                    ),
                )
            if removed:
                placeholders = ", ".join("?" for _ in removed)
                conn.execute(f"DELETE FROM intake_ledger WHERE id IN ({placeholders})", tuple(removed))
            count = conn.execute("SELECT COUNT(*) FROM intake_ledger").fetchone()[0]
            recheck = conn.execute("SELECT value FROM intake_control WHERE key = 'recheck_requested'").fetchone()
            conn.commit()
        return {"added": added, "removed": removed, "current_count": int(count), "recheck_requested": bool(recheck and recheck["value"] == "1")}

    def list_intake_candidates(self) -> list[dict[str, Any]]:
        """Return all current intake ledger cards; callers perform deterministic sorting."""

        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM intake_ledger ORDER BY id ASC").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def set_intake_recheck_requested(self, requested: bool) -> None:
        self.init_schema()
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO intake_control(key, value, updated_at) VALUES ('recheck_requested', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                ("1" if requested else "0", now),
            )
            conn.commit()

    def pop_intake_recheck_requested(self) -> bool:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM intake_control WHERE key = 'recheck_requested'").fetchone()
            requested = bool(row and row["value"] == "1")
            conn.execute("DELETE FROM intake_control WHERE key = 'recheck_requested'")
            conn.commit()
        return requested

    # -----------------------------------------------------------------------
    # Stage 4 — durable outbox
    # -----------------------------------------------------------------------

    def enqueue_event(
        self,
        event_type: str,
        source_id: str,
        source: str,
        payload: dict[str, Any],
    ) -> int:
        """Backward-compatible alias for ``queue_for_dispatch``."""
        return self.queue_for_dispatch(
            event_type=event_type,
            source_id=source_id,
            source=source,
            payload=payload,
        )

    def queue_for_dispatch(
        self,
        *,
        event_type: str,
        source_id: str,
        source: str,
        payload: dict[str, Any],
    ) -> int:
        """Durably queue one source event, idempotent by ``(event_type, source_id)``.

        The returned row is never duplicated across poll cycles.  Terminal and
        in-flight rows are kept as idempotency tombstones; retries after an
        ``abandoned`` row require an explicit future approval path, not silent
        redrive.
        """
        self.init_schema()
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    created_at, updated_at, event_type, source_id, source,
                    payload_json, state, queued_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    now,
                    now,
                    event_type,
                    source_id,
                    source,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            existing = conn.execute(
                "SELECT id FROM outbox WHERE event_type = ? AND source_id = ?",
                (event_type, source_id),
            ).fetchone()
            if existing is None:
                raise RuntimeError("failed to create or fetch outbox row")
            row_id = int(existing["id"])
            conn.commit()
        return row_id

    def list_outbox_events(self, *, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Return outbox events ordered by id, optionally filtered by state."""
        self.init_schema()
        query = "SELECT * FROM outbox"
        params: tuple[Any, ...] = ()
        if states:
            placeholders = ", ".join("?" for _ in states)
            query += f" WHERE state IN ({placeholders})"
            params = tuple(states)
        query += " ORDER BY id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_outbox_row(row) for row in rows]

    def list_pending_events(self) -> list[dict[str, Any]]:
        """Backward-compatible name: return queued events."""
        return self.list_outbox_events(states=("queued",))

    def get_outbox_event(self, event_id: int) -> dict[str, Any]:
        """Fetch a single outbox event by id."""
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"outbox event not found: {event_id}")
        return self._decode_outbox_row(row)

    def get_outbox_event_by_source(self, event_type: str, source_id: str) -> dict[str, Any] | None:
        """Fetch an outbox event by its idempotency key."""
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE event_type = ? AND source_id = ?",
                (event_type, source_id),
            ).fetchone()
        return None if row is None else self._decode_outbox_row(row)

    def mark_event_dispatching(self, event_id: int) -> None:
        """Mark an event dispatching before any worker side effect starts."""
        self._transition_outbox_event(event_id, "dispatching", increment_attempt=True)

    def mark_event_mj_review(self, event_id: int, *, reason: str) -> None:
        """Hold an event for MJ review before any dispatch attempt."""
        self._transition_outbox_event(event_id, "mj_review", reason=reason, escalate=True)

    def mark_event_succeeded(self, event_id: int) -> None:
        """Mark an outbox event succeeded."""
        self._transition_outbox_event(event_id, "succeeded")

    def mark_event_processed(self, event_id: int) -> None:
        """Backward-compatible alias for success."""
        self.mark_event_succeeded(event_id)

    def mark_event_failed(self, event_id: int, *, reason: str = "") -> None:
        """Mark an outbox event failed with an optional reason."""
        self._transition_outbox_event(event_id, "failed", reason=reason)

    def reconcile_in_flight(self, *, reason: str = "reconcile-on-restart") -> list[dict[str, Any]]:
        """Mark any in-flight dispatches abandoned and requiring MJ review.

        Reconciliation is deliberately fail-closed: a previously ``dispatching``
        row is not silently queued again because the worker may have performed
        an external side effect before the daemon died.
        """
        self.init_schema()
        now = int(time.time())
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'dispatching' ORDER BY id ASC"
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE outbox
                    SET state = 'abandoned', updated_at = ?, completed_at = ?,
                        last_error = ?, escalation_required = 1
                    WHERE id = ?
                    """,
                    (now, now, reason, int(row["id"])),
                )
            conn.commit()
        return [self.get_outbox_event(int(row["id"])) for row in rows]

    def _transition_outbox_event(
        self,
        event_id: int,
        state: str,
        *,
        reason: str = "",
        increment_attempt: bool = False,
        escalate: bool = False,
    ) -> None:
        now = int(time.time())
        completed = now if state in {"succeeded", "failed", "abandoned"} else None
        dispatching = now if state == "dispatching" else None
        escalation = 1 if state == "abandoned" or escalate else 0
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE outbox
                SET state = ?, updated_at = ?,
                    attempt_count = attempt_count + ?,
                    dispatching_at = COALESCE(?, dispatching_at),
                    completed_at = COALESCE(?, completed_at),
                    last_error = NULLIF(?, ''),
                    escalation_required = CASE WHEN ? = 1 THEN 1 ELSE escalation_required END
                WHERE id = ?
                """,
                (
                    state,
                    now,
                    1 if increment_attempt else 0,
                    dispatching,
                    completed,
                    reason,
                    escalation,
                    event_id,
                ),
            )
            if cur.rowcount != 1:
                raise KeyError(f"outbox event not found: {event_id}")
            conn.commit()

    @staticmethod
    def _decode_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        data["escalation_required"] = bool(data.get("escalation_required", 0))
        data["status"] = data["state"]  # compatibility for older callers/tests
        return data

