"""Local SQLite state for Team OS Phase 1 snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

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
