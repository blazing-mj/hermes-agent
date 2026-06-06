"""Ephemeral Worker runner for Team OS — Stage 3 (AGENTS-177).

One ticket → isolated git worktree → lease → executor → release.
Human gate always ON; no auto-dispatch, no auto-Done, no loop feed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

Executor = Callable[[str, Path, float], str]

_CLAUDE_MAX_CODE = os.environ.get(
    "CLAUDE_MAX_CODE",
    str(Path.home() / ".hermes" / "bin" / "claude-max-code"),
)

_DENYLIST: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.env", re.IGNORECASE), "env-secrets"),
    (re.compile(r"cred(ential)?", re.IGNORECASE), "credentials"),
    (re.compile(r"\bmoney\b", re.IGNORECASE), "money/billing"),
    (re.compile(r"(^|[/\\])prod([/\\]|$)", re.IGNORECASE), "prod/production"),
    (re.compile(r"gateway[/\\]runtime", re.IGNORECASE), "live-gateway-runtime"),
]


def check_worker_boundary(
    *,
    repo_root: Path,
    worktree_path: Path,
    worktree_root: Path,
    allowed_files: list[str],
) -> list[str]:
    """Return boundary violations. Empty list means the boundary is clean."""
    violations: list[str] = []

    try:
        if worktree_path.resolve() == repo_root.resolve():
            violations.append(
                "not using isolated worktree: worktree_path is the live repo root"
            )
    except Exception:
        pass

    try:
        worktree_path.resolve().relative_to(worktree_root.resolve())
    except ValueError:
        violations.append(
            f"worktree is outside worktree root: {worktree_path} not under {worktree_root}"
        )

    for file_path in allowed_files:
        for pattern, label in _DENYLIST:
            if pattern.search(file_path):
                violations.append(f"denied path: {file_path} — matches {label} denylist")
                break

    return violations


def _build_worker_prompt(contract: dict[str, Any]) -> str:
    return "\n".join([
        "You are a Team OS Worker. Work on the task described in the contract below.",
        "Work only in this isolated git worktree. Do not touch live runtime, gateway, or production paths.",
        "Do not mark Linear Done. Human gate remains required.",
        "",
        "CONTRACT:",
        json.dumps(contract, indent=2, sort_keys=True),
        "",
        "Complete the implementation and run the required proof commands.",
    ])


def _default_executor(prompt: str, cwd: Path, timeout: float) -> str:
    result = subprocess.run(
        (
            _CLAUDE_MAX_CODE,
            "team-os-worker-runner",
            "--",
            "-p",
            prompt,
            "--model",
            "opus",
            "--max-turns",
            "10",
        ),
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return (result.stdout or "").strip()


def _acquire_lease(lease_path: Path, ticket: str, *, ttl_seconds: float) -> None:
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if lease_path.exists():
        try:
            existing = json.loads(lease_path.read_text(encoding="utf-8"))
            expires_at = float(existing.get("expires_at", 0))
        except Exception:
            expires_at = 0.0
        if expires_at > now:
            raise RuntimeError(f"worker lease already active for {lease_path}")
    lease_path.write_text(
        json.dumps(
            {
                "owner": "team-os-worker-runner",
                "pid": os.getpid(),
                "ticket": ticket,
                "acquired_at": now,
                "expires_at": now + ttl_seconds,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _release_lease(lease_path: Path) -> None:
    try:
        lease_path.unlink()
    except OSError:
        pass


def _get_changed_files(worktree_path: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--name-only"],
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
        )
        return sorted(f.strip() for f in result.stdout.splitlines() if f.strip())
    except Exception:
        return []


def _run_proof_commands(
    contract: dict[str, Any],
    worktree_path: Path,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    commands = contract.get("required_commands", [])
    if not isinstance(commands, list):
        return results
    per_command_timeout = max(1.0, min(timeout_seconds, 120.0))
    for raw in commands:
        if not isinstance(raw, str) or not raw.strip():
            continue
        command = raw.strip()
        run_command = command
        if command.startswith("python "):
            run_command = f"{sys.executable} {command[len('python '):]}"
        try:
            completed = subprocess.run(
                run_command,
                cwd=worktree_path,
                text=True,
                shell=True,
                capture_output=True,
                timeout=per_command_timeout,
                check=False,
            )
            results.append(
                {
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    "command": command,
                    "exit_code": -1,
                    "stdout": exc.stdout or "",
                    "stderr": "proof command timed out",
                }
            )
    return results


def _handoff_base(source_ticket: str, worktree_path: Path) -> dict[str, Any]:
    return {
        "schema": "team_os.worker_handoff.v1",
        "source_ticket": source_ticket,
        "worktree_path": str(worktree_path),
        "changed_files": [],
        "proof_results": [],
        "worker_output": None,
        "human_gate_required": True,
        "loop_feed_allowed": False,
        "auto_dispatch_allowed": False,
        "auto_done_allowed": False,
    }


def run_worker(
    *,
    contract: dict[str, Any],
    repo_root: Path,
    worktree_root: Path,
    lease_path: Path,
    branch: str,
    timeout_seconds: float = 600.0,
    executor: Executor | None = None,
) -> dict[str, Any]:
    """Run one ephemeral Worker in an isolated git worktree."""
    source_ticket = str(contract.get("source_ticket", "unknown"))
    safe_branch = branch.replace("/", "-").replace("\\", "-")
    worktree_path = worktree_root / safe_branch
    worktree_root.mkdir(parents=True, exist_ok=True)

    allowed_files = [str(item) for item in contract.get("files_to_touch", []) if isinstance(item, str)]
    boundary_violations = check_worker_boundary(
        repo_root=repo_root,
        worktree_path=worktree_path,
        worktree_root=worktree_root,
        allowed_files=allowed_files,
    )
    if boundary_violations:
        result = _handoff_base(source_ticket, worktree_path)
        result.update(
            {
                "worker_status": "boundary_denied",
                "boundary_violations": boundary_violations,
            }
        )
        return result

    try:
        _acquire_lease(lease_path, source_ticket, ttl_seconds=timeout_seconds)
    except RuntimeError as exc:
        result = _handoff_base(source_ticket, worktree_path)
        result.update({"worker_status": "lease_denied", "error": str(exc)})
        return result

    try:
        subprocess.run(
            ["git", "worktree", "add", "-b", safe_branch, str(worktree_path)],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        _release_lease(lease_path)
        raw_error = exc.stderr or exc.output or str(exc)
        error = raw_error.decode() if isinstance(raw_error, bytes) else str(raw_error)
        result = _handoff_base(source_ticket, worktree_path)
        result.update({"worker_status": "worktree_failed", "error": error})
        return result

    try:
        prompt = _build_worker_prompt(contract)
        _executor = executor or _default_executor
        worker_output = _executor(prompt, worktree_path, timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - convert worker crash into handoff
        result = _handoff_base(source_ticket, worktree_path)
        result.update(
            {
                "worker_status": "failed",
                "error": str(exc),
                "changed_files": _get_changed_files(worktree_path),
            }
        )
        return result
    finally:
        _release_lease(lease_path)

    if not (worker_output or "").strip():
        result = _handoff_base(source_ticket, worktree_path)
        result.update(
            {
                "worker_status": "fallback_required",
                "fallback_reason": "worker produced no output",
            }
        )
        return result

    proof_results = _run_proof_commands(contract, worktree_path, timeout_seconds)
    changed_files = _get_changed_files(worktree_path)

    result = _handoff_base(source_ticket, worktree_path)
    result.update(
        {
            "worker_status": "completed",
            "changed_files": changed_files,
            "proof_results": proof_results,
            "worker_output": worker_output,
        }
    )
    return result


def write_result(result: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(str(output))
    else:
        print(rendered, end="")
