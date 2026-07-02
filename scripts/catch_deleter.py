#!/usr/bin/env python3
"""catch_deleter.py — high-frequency forensic watcher to catch the UNLOGGED
tracked-file deleter in the act.

Watches every git-tracked tests/*.py across all worktrees at 0.5s resolution.
The instant one vanishes, it snapshots (within ~0.5s) the live process table,
recent agent/gateway log tails, active gateway sessions, and the worktree reflog
— i.e. exactly what was running at the moment of deletion — then restores the
file from HEAD and exits so the evidence is delivered immediately.

Read-only except for the one-shot git restore after capture. Not a heal-loop.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

REPO = Path("/Users/alfred/.hermes/hermes-agent")
OUT = Path("/Users/alfred/.hermes/logs/catch-deleter.log")
RUN_SECONDS = int(os.environ.get("CATCH_SECONDS", "7200"))  # 2h default
POLL = 0.5


def sh(args: list[str], cwd: Path | None = None, timeout: int = 20) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:  # noqa: BLE001
        return f"__ERR__ {e}"


def log(msg: str) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def worktrees() -> list[Path]:
    out = sh(["git", "worktree", "list", "--porcelain"], cwd=REPO)
    return [Path(l.split(" ", 1)[1].strip()) for l in out.splitlines() if l.startswith("worktree ")]


def vulnerable_tests(wt: Path, recent_cutoff: float) -> list[Path]:
    """Test files this worktree TOUCHED vs its merge-base with main — i.e. the
    worker's own tests, which are exactly what the deleter removes. Only for
    worktrees with a recent HEAD (active work)."""
    head = sh(["git", "log", "-1", "--format=%ct"], cwd=wt).strip()
    if not (head.isdigit() and int(head) > recent_cutoff):
        return []
    base = sh(["git", "merge-base", "main", "HEAD"], cwd=wt).strip() or "main"
    touched = sh(["git", "diff", "--name-only", f"{base}..HEAD", "--", "tests/"], cwd=wt)
    return [wt / rel for rel in touched.splitlines() if rel.endswith(".py")]


def capture(path: Path, wt: Path) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    # Full command lines for candidate deleters. macOS ps `command` is the full argv string.
    ps = sh(["ps", "-axo", "pid,ppid,etime,command"])
    hot = "\n".join(
        l for l in ps.splitlines()
        if any(k in l for k in ("hermes", "codex", "delegate", "claude", "git ", "python", " rm ", "/rm ", "bash", "zsh"))
    )
    tails = sh(["bash", "-lc",
                "for f in /Users/alfred/.hermes/logs/agent.log /Users/alfred/.hermes/logs/tracked-delete-guard.jsonl /Users/alfred/.hermes/profiles/*/logs/agent.log; "
                "do echo \"== $f ==\"; tail -n 12 \"$f\" 2>/dev/null; done"])
    reflog = sh(["git", "reflog", "--date=iso", "-n", "4"], cwd=wt)
    log("\n" + "=" * 60)
    log(f"DELETION CAUGHT  [{ts}]")
    log(f"FILE     : {path}")
    log(f"WORKTREE : {wt}")
    log("--- HOT PROCESSES at moment of deletion (full command lines) ---")
    log(hot or "(none matched)")
    log("--- worktree reflog (was it git?) ---")
    log(reflog)
    log("--- recent agent/gateway log tails (active session?) ---")
    log(tails)
    log("=" * 60)


def main() -> int:
    recent_cutoff = time.time() - 3 * 86400
    watch: dict[str, Path] = {}
    for wt in worktrees():
        for p in vulnerable_tests(wt, recent_cutoff):
            watch[str(p)] = wt
    log(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] catch_deleter START — watching {len(watch)} worker-touched test files, poll={POLL}s, window={RUN_SECONDS}s")
    for p in watch:
        log(f"  watch: {p}")

    present = {p: Path(p).exists() for p in watch}
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        time.sleep(POLL)
        for p, wt in watch.items():
            exists = Path(p).exists()
            if present[p] and not exists:
                capture(Path(p), wt)
                rel = str(Path(p).relative_to(wt))
                sh(["git", "checkout", "HEAD", "--", rel], cwd=wt)
                log(f"[restored {rel} from HEAD; exiting with evidence]")
                return 0
            present[p] = exists
    log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] catch_deleter END — no deletion in window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
