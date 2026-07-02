#!/usr/bin/env python3
"""worktree_integrity_check.py — canary for recurring git-tracked file loss.

Read-only. For every git worktree of the hermes-agent repo, report any tracked
file that is missing from disk (git status 'D'). Also flag critical live helper
files that must be backed by committed repo files. Exit 1 if anything is wrong,
so launchd/cron can alert immediately.
"""
from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path("/Users/alfred/.hermes/hermes-agent")
# Live files the Team OS board depends on that MUST be recoverable from git.
CRITICAL_LIVE = [
    Path("/Users/alfred/.hermes/scripts/restricted_linear_writer.py"),
    Path("/Users/alfred/.hermes/scripts/team_os_lane_watcher.py"),
    Path("/Users/alfred/.hermes/scripts/verify_board_flow.py"),
]


def run(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30, shell=False).stdout
    except Exception as e:  # noqa: BLE001 - tripwire should report, not crash
        return f"__ERR__ {e}"


def worktrees() -> list[Path]:
    out = run(["git", "worktree", "list", "--porcelain"], REPO)
    return [Path(l.split(" ", 1)[1].strip()) for l in out.splitlines() if l.startswith("worktree ")]


def tracked_anywhere(p: Path) -> bool:
    """Is a repo file with this critical helper basename tracked?"""
    out = run(["git", "ls-files", "--", f"*/{p.name}", p.name], REPO)
    return bool(out.strip())


def _samefile_or_symlink_to_repo(p: Path) -> bool:
    try:
        target = p.resolve(strict=True)
    except OSError:
        return False
    try:
        target.relative_to(REPO)
    except ValueError:
        return False
    return tracked_anywhere(target)


def collect_problems() -> list[str]:
    problems: list[str] = []

    for wt in worktrees():
        status = run(["git", "status", "--porcelain"], wt)
        if status.startswith("__ERR__"):
            problems.append(f"[worktree unreadable] {wt}: {status}")
            continue
        for line in status.splitlines():
            code, path = line[:2], line[3:]
            if "D" in code:  # tracked file deleted from working tree
                problems.append(f"[TRACKED FILE DELETED] {wt}/{path}  (recover: git -C {wt} checkout HEAD -- {path})")

    for live in CRITICAL_LIVE:
        if not live.exists():
            problems.append(f"[CRITICAL LIVE FILE MISSING] {live}")
        elif not tracked_anywhere(live) and not _samefile_or_symlink_to_repo(live):
            problems.append(f"[VANISH RISK — not in git] {live}  (commit it into the repo and symlink/copy live path)")
    return problems


def maybe_alert(problems: list[str], output: str) -> None:
    if not problems:
        return
    # macOS user notification.  This is best-effort; stdout/stderr logs remain
    # the durable alert channel for launchd even if Notification Center is off.
    title = "Hermes worktree integrity alert"
    message = f"{len(problems)} tracked-file integrity problem(s). Check ~/.hermes/logs/worktree-integrity.log"
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {message!r} with title {title!r}'],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except Exception:
        pass


def render(problems: list[str]) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n  [{stamp}] worktree integrity check"]
    if not problems:
        lines.append("  \033[92mOK\033[0m — no deleted tracked files; critical live files backed by git.\n")
    else:
        lines.extend(f"  \033[91m✗\033[0m {p}" for p in problems)
        lines.append(f"\n  {len(problems)} problem(s).\n")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert", action="store_true", help="send macOS notification on failures")
    args = parser.parse_args(argv)
    problems = collect_problems()
    output = render(problems)
    print(output)
    if args.alert:
        maybe_alert(problems, output)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
