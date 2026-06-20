"""Tests for corrupt-kanban-DB recovery (``kanban_db.recover_corrupt_db``).

These exercise the data-preserving recovery path that backs ``hermes kanban
recover`` and the dispatcher's auto-heal: induce *real* SQLite corruption
(zero an interior b-tree page so ``integrity_check`` fails but ``.recover``
can still salvage the surviving pages), then assert the recovery salvages
tasks, swaps a clean DB in atomically, preserves a backup of the corrupt
original, and drops stale WAL sidecars.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _build_board(tmp_path: Path, monkeypatch, n_tasks: int = 60) -> Path:
    """Create a healthy ``default`` board under a tmp HERMES_HOME with N tasks,
    checkpointed into the main DB file so corruption lands on real data."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    conn = kb.connect(board="default")
    try:
        for i in range(n_tasks):
            kb.create_task(conn, title=f"task {i}", body="payload " * 40)
        # Flush WAL into the main DB file so the bytes we corrupt are real rows.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    return path


def _corrupt_one_interior_page(path: Path) -> None:
    """Zero a single interior page until ``integrity_check`` fails.

    Keeps page 1 (header + schema) and page 2 (likely the tasks root) intact and
    zeroes a later leaf page, so the corruption is real but ``.recover`` can
    still salvage the surviving rows. Asserts the precondition actually took, so
    the test can never silently pass against an uncorrupted DB.
    """
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size <= 1:  # SQLite encodes 65536 as the value 1
        page_size = 65536
    n_pages = len(raw) // page_size
    assert n_pages >= 4, f"need >=4 pages to corrupt a leaf, got {n_pages}"
    # Later pages first — more likely leaves holding rows than interior/root pages.
    for idx in range(n_pages - 1, 1, -1):
        trial = bytearray(raw)
        start = idx * page_size
        trial[start : start + page_size] = b"\x00" * page_size
        path.write_bytes(bytes(trial))
        if not kb._integrity_is_ok(path):
            return
    raise AssertionError("could not induce integrity_check failure by zeroing pages")


def test_recover_salvages_tasks_and_swaps_clean_db(tmp_path, monkeypatch):
    path = _build_board(tmp_path, monkeypatch)
    healthy_count = kb._count_tasks(path)
    assert healthy_count == 60

    _corrupt_one_interior_page(path)
    assert not kb._integrity_is_ok(path)  # precondition: really corrupt

    result = kb.recover_corrupt_db(path)

    assert result.recovered is True, result.reason
    assert result.tasks >= 1
    # The live file is now a clean, openable DB.
    assert kb._integrity_is_ok(path)
    # A backup of the corrupt original was preserved, and it is the corrupt one.
    assert result.backup_path is not None and result.backup_path.exists()
    assert not kb._integrity_is_ok(result.backup_path)
    # Stale WAL/SHM sidecars are gone so SQLite can't replay them onto the new DB.
    assert not (path.parent / (path.name + "-wal")).exists()
    assert not (path.parent / (path.name + "-shm")).exists()

    # End-to-end: the board opens through the normal guarded connect() and the
    # task count matches what recovery reported.
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    with kb.connect(board="default") as conn:
        live = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert live == result.tasks


def test_recover_is_noop_on_healthy_db(tmp_path, monkeypatch):
    path = _build_board(tmp_path, monkeypatch, n_tasks=5)
    before = path.read_bytes()

    result = kb.recover_corrupt_db(path)

    assert result.recovered is False
    assert "healthy" in result.reason
    assert result.tasks == 5
    # A healthy DB is left byte-for-byte untouched.
    assert path.read_bytes() == before


def test_recover_removes_stale_wal_sidecar(tmp_path, monkeypatch):
    """The WAL-safe swap must drop a pre-existing stale -wal so it cannot be
    replayed onto the freshly recovered DB (the #1 corruption-amplifier)."""
    path = _build_board(tmp_path, monkeypatch)
    _corrupt_one_interior_page(path)
    # Plant a stale WAL sidecar alongside the corrupt DB.
    wal = path.parent / (path.name + "-wal")
    wal.write_bytes(b"stale-wal-bytes")
    assert wal.exists()

    result = kb.recover_corrupt_db(path)

    assert result.recovered is True, result.reason
    assert not wal.exists()
    assert kb._integrity_is_ok(path)


def test_recover_refuses_to_swap_empty_over_corrupt(tmp_path, monkeypatch):
    """If ``.recover`` salvages nothing, recovery must refuse the swap rather
    than wipe a DB that may have held data — silent loss is worse than staying
    quarantined. The live file must be left untouched."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    path = kb.kanban_db_path(board="default")
    path.parent.mkdir(parents=True, exist_ok=True)
    garbage = b"this is not a sqlite database at all" * 64
    path.write_bytes(garbage)
    assert not kb._integrity_is_ok(path)

    result = kb.recover_corrupt_db(path)

    assert result.recovered is False
    # Live file untouched — no silent data loss, no swap.
    assert path.read_bytes() == garbage


def test_recover_missing_file_is_safe(tmp_path):
    result = kb.recover_corrupt_db(tmp_path / "nope.db")
    assert result.recovered is False
    assert "missing or empty" in result.reason
