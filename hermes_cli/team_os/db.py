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

