#!/usr/bin/env python3
"""Build a deterministic ledger for EMA child-output contract attempts.

This is an offline verifier/test-support tool.  It reads copied/redacted EMA
session JSONL snippets and checks each expected child output path against a local
artifact tree.  It does not mutate OpenClaw state or call live APIs.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_OUTPUT_RE = re.compile(r"(?:Path|Output(?: path)?):\s*`?([^`\n]+\.md)`?", re.IGNORECASE)
_DONE_RE = re.compile(r"DONE\.\s*Deliverable:\s*([^\s]+\.md)", re.IGNORECASE)
_SESSION_RE = re.compile(r"session_key:\s*([^\s]+)")
_ENOENT_RE = re.compile(r"access '([^']+\.md)'")

FAIL_CLOSED = {"MISSING_OUTPUT", "NO_STATUS"}
STATUS_VALUES = {"OK", "PARTIAL", "BLOCKED"}


class Attempt:
    def __init__(
        self,
        *,
        attempt: int,
        agent_id: str,
        child_session_key: str,
        run_id: str,
        output_path: str,
        spawned_at: str,
    ) -> None:
        self.attempt = attempt
        self.agent_id = agent_id
        self.child_session_key = child_session_key
        self.run_id = run_id
        self.output_path = output_path
        self.spawned_at = spawned_at
        self.completed_at = ""
        self.parent_read_error = False

    @property
    def output_name(self) -> str:
        return Path(self.output_path).name


def _loads(line: str) -> dict[str, Any] | None:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _content_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    items = message.get("content") or []
    return items if isinstance(items, list) else []


def _text_from_message(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in _content_items(message):
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
    return "\n".join(chunks)


def _extract_output_path(task: str) -> str:
    match = _OUTPUT_RE.search(task or "")
    return match.group(1).strip() if match else ""


def _local_artifact_path(output_path: str, artifact_root: Path) -> Path:
    """Map an absolute OpenClaw output path to the redacted fixture artifact tree."""
    return artifact_root / Path(output_path).name


def _artifact_status(output_path: str, artifact_root: Path) -> tuple[str, str]:
    path = _local_artifact_path(output_path, artifact_root)
    if not path.exists():
        return "MISSING_OUTPUT", ""
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except IndexError:
        first_line = ""
    if not first_line.startswith("STATUS:"):
        return "NO_STATUS", ""
    value = first_line.split(":", 1)[1].strip().split()[0].strip("|").upper()
    if value in STATUS_VALUES:
        return value, first_line
    return "NO_STATUS", first_line


def _iter_events(session_jsonl: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in session_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = _loads(line)
        if event is not None:
            events.append(event)
    return events


def build_ledger(session_jsonl: Path | str, artifact_root: Path | str) -> list[dict[str, Any]]:
    session_path = Path(session_jsonl)
    artifact_dir = Path(artifact_root)
    pending_spawns: dict[str, dict[str, Any]] = {}
    attempts: list[Attempt] = []
    attempts_by_child: dict[str, Attempt] = {}
    attempts_by_output: dict[str, list[Attempt]] = {}

    for event in _iter_events(session_path):
        message = event.get("message") or {}
        timestamp = str(event.get("timestamp", ""))

        for item in _content_items(message):
            if item.get("type") != "toolCall" or item.get("name") != "sessions_spawn":
                continue
            args = item.get("arguments") or {}
            output_path = _extract_output_path(str(args.get("task", "")))
            pending_spawns[str(item.get("id"))] = {
                "agent_id": str(args.get("agentId", "")),
                "output_path": output_path,
                "spawned_at": timestamp,
            }

        if message.get("role") == "toolResult" and message.get("toolName") == "sessions_spawn":
            call_id = str(message.get("toolCallId", ""))
            pending = pending_spawns.pop(call_id, None)
            details = message.get("details") or {}
            if pending and details.get("status") == "accepted" and pending.get("output_path"):
                output_path = str(pending["output_path"])
                attempt_no = len(attempts_by_output.get(output_path, [])) + 1
                attempt = Attempt(
                    attempt=attempt_no,
                    agent_id=str(pending.get("agent_id", "")),
                    child_session_key=str(details.get("childSessionKey", "")),
                    run_id=str(details.get("runId", "")),
                    output_path=output_path,
                    spawned_at=str(pending.get("spawned_at", "")),
                )
                attempts.append(attempt)
                attempts_by_child[attempt.child_session_key] = attempt
                attempts_by_output.setdefault(output_path, []).append(attempt)

        if message.get("role") == "user":
            text = _text_from_message(message)
            session_match = _SESSION_RE.search(text)
            done_match = _DONE_RE.search(text)
            child_key = session_match.group(1) if session_match else ""
            if child_key in attempts_by_child:
                attempts_by_child[child_key].completed_at = timestamp
            elif done_match:
                output_path = done_match.group(1)
                candidates = attempts_by_output.get(output_path, [])
                if candidates:
                    candidates[-1].completed_at = timestamp

        if message.get("role") == "toolResult" and message.get("toolName") == "read":
            details = message.get("details") or {}
            error = str(details.get("error", ""))
            path_match = _ENOENT_RE.search(error)
            if path_match:
                output_path = path_match.group(1)
                candidates = attempts_by_output.get(output_path, [])
                completed = [a for a in candidates if a.completed_at]
                if completed:
                    completed[-1].parent_read_error = True

    ledger: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.parent_read_error:
            status, header, evidence = "MISSING_OUTPUT", "", "parent_read_error_after_completion"
        else:
            status, header = _artifact_status(attempt.output_path, artifact_dir)
            evidence = "artifact_status_header" if header else "artifact_missing_or_no_status"
        ledger.append(
            {
                "agent_id": attempt.agent_id,
                "attempt": attempt.attempt,
                "child_session_key": attempt.child_session_key,
                "completed_at": attempt.completed_at,
                "evidence": evidence,
                "fail_closed": status in FAIL_CLOSED,
                "output_name": attempt.output_name,
                "output_path": attempt.output_path,
                "run_id": attempt.run_id,
                "spawned_at": attempt.spawned_at,
                "status": status,
                "status_header": header,
            }
        )
    return ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify EMA child output artifacts from redacted session JSONL")
    parser.add_argument("--session-jsonl", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    args = parser.parse_args(argv)

    ledger = build_ledger(args.session_jsonl, args.artifact_root)
    print(json.dumps(ledger, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
