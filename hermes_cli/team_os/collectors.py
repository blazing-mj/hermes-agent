"""Read-only collectors for Team OS Phase 1."""

from __future__ import annotations

import csv
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .schema import Observation


def collect_linear_issues_from_text(text: str, *, collected_at: int | None = None) -> list[Observation]:
    """Parse `linear-agent list` output.

    Expected line shape:
    `AGENTS-64 | In Progress | Hermes System | Title | label1,label2 | url`
    """

    ts = int(time.time()) if collected_at is None else collected_at
    observations: list[Observation] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or " | " not in line:
            continue
        parts = [part.strip() for part in line.split(" | ")]
        if len(parts) < 6 or not parts[0].startswith("AGENTS-"):
            continue
        identifier, status, project, title, labels_raw, url = parts[:6]
        labels = [label.strip() for label in labels_raw.split(",") if label.strip()]
        observations.append(
            Observation(
                source="linear",
                source_id=identifier,
                title=title,
                body=None,
                status=status,
                project=project,
                labels=labels,
                url=url,
                collected_at=ts,
            )
        )
    return observations


def collect_linear_project(
    project: str,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    helper_path: str | Path = "~/.hermes/bin/linear-agent",
) -> list[Observation]:
    """Collect Linear issues using the read-only local helper list command."""

    helper = str(Path(helper_path).expanduser())
    cmd = [helper, "list", "--project", project]
    if runner is None:
        completed = subprocess.run(cmd, text=True, capture_output=True, check=True)
    else:
        completed = runner(cmd)
    return collect_linear_issues_from_text(completed.stdout)


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_kanban_tasks_from_db(
    db_path: str | Path,
    *,
    board: str,
    include_archived: bool = False,
    limit: int | None = None,
    collected_at: int | None = None,
) -> list[Observation]:
    """Collect Kanban tasks from SQLite in read-only mode."""

    ts = int(time.time()) if collected_at is None else collected_at
    query = "SELECT id, title, body, status, tenant, assignee, created_by FROM tasks"
    params: list[object] = []
    if not include_archived:
        query += " WHERE status != ?"
        params.append("archived")
    query += " ORDER BY priority DESC, created_at ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    with _connect_readonly(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    observations: list[Observation] = []
    for row in rows:
        labels = []
        if row["tenant"]:
            labels.append(f"tenant:{row['tenant']}")
        if row["assignee"]:
            labels.append(f"assignee:{row['assignee']}")
        observations.append(
            Observation(
                source=f"kanban:{board}",
                source_id=row["id"],
                title=row["title"],
                body=row["body"],
                status=row["status"],
                project=board,
                labels=labels,
                url=None,
                collected_at=ts,
            )
        )
    return observations


def collect_kanban_board(board: str, *, limit: int | None = None) -> list[Observation]:
    """Resolve a Hermes Kanban board DB path and collect tasks read-only."""

    from hermes_cli import kanban_db

    path = kanban_db.kanban_db_path(board)
    if not path.exists():
        return []
    return collect_kanban_tasks_from_db(path, board=board, limit=limit)


def collect_observations(
    *,
    linear_projects: Iterable[str] = (),
    kanban_boards: Iterable[str] = (),
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    limit_per_kanban_board: int | None = None,
) -> list[Observation]:
    """Collect read-only observations from requested sources."""

    observations: list[Observation] = []
    for project in linear_projects:
        observations.extend(collect_linear_project(project, runner=runner))
    for board in kanban_boards:
        observations.extend(collect_kanban_board(board, limit=limit_per_kanban_board))
    return observations
