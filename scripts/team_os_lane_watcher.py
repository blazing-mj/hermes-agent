#!/usr/bin/env python3
"""Team OS Phase 2 lane watcher and single-card lease ledger.

This is a deterministic control-plane helper for AGENTS-191.  It does not make
raw Linear mutations.  All lane movement goes through the Phase-1 gated
restricted_linear_writer.py surface, which enforces board-transitions.json.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HERMES_HOME = Path.home() / ".hermes"
LINEAR_AGENT = "/Users/alfred/.hermes/bin/linear-agent"
RESTRICTED_WRITER_PATH = HERMES_HOME / "scripts" / "restricted_linear_writer.py"
DEFAULT_LEDGER_DIR = HERMES_HOME / "state"
PROJECT_DEFAULT = "Hermes System"

Runner = Callable[[list[str], str | None], str]

ROUTES: dict[tuple[str, str], dict[str, Any]] = {
    ("cortex", "Backlog"): {
        "to": "Triage",
        "conditions": [],
        "assignee": "",
    },
    ("cto", "Todo"): {
        "to": "In Progress",
        "conditions": [],
        "assignee": "",
    },
    # Triage handoff is exposed as an explicit advance path because it requires
    # artifacts, not a blind lane poll.
    ("cortex", "Triage"): {
        "to": "Todo",
        "conditions": ["grounding_and_contract_present", "assignee_set_cto", "human_gate_label_absent"],
        "assignee": "cto",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat(timespec="seconds")


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True))
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fh is not None:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


def _lease_paths(ledger_dir: Path) -> tuple[Path, Path, Path]:
    return (
        ledger_dir / "team-os-card-leases.lock",
        ledger_dir / "team-os-card-leases.json",
        ledger_dir / "team-os-card-leases.jsonl",
    )


def _expired(record: dict[str, Any]) -> bool:
    try:
        expires = datetime.fromisoformat(str(record.get("expires_at")))
    except Exception:
        return True
    return expires <= _now()


def claim_card(*, issue: str, lane: str, holder: str, ledger_dir: str | Path = DEFAULT_LEDGER_DIR,
               ttl_seconds: int = 900) -> dict[str, Any]:
    """Atomically lease one Linear card.

    The lease key is the card identifier.  A non-expired existing lease blocks all
    other holders, which proves two simultaneous ticks cannot both claim the same
    card.  Expired leases are reclaimed under the same file lock.
    """
    ledger_path = Path(ledger_dir)
    lock_path, compact_path, event_path = _lease_paths(ledger_path)
    issue = str(issue).strip()
    lane = str(lane).strip()
    holder = str(holder).strip()
    if not issue or not lane or not holder:
        raise ValueError("issue, lane, and holder are required")

    with FileLock(lock_path):
        data = _read_json(compact_path, {"schema": "team_os.card_leases.v1", "leases": {}})
        leases = data.setdefault("leases", {})
        existing = leases.get(issue)
        if existing and not _expired(existing):
            result = {
                "claimed": False,
                "reason": "already_leased",
                "issue": issue,
                "lane": lane,
                "holder": holder,
                "existing_holder": existing.get("holder"),
                "lease_id": existing.get("lease_id"),
            }
            _append_jsonl(event_path, {"event": "claim_denied", "recorded_at": _now_text(), **result})
            return result

        lease_id = hashlib.sha256(f"{issue}:{lane}:{holder}:{uuid.uuid4()}".encode()).hexdigest()[:16]
        now = _now()
        record = {
            "issue": issue,
            "lane": lane,
            "holder": holder,
            "lease_id": lease_id,
            "claimed_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
            "status": "active",
        }
        leases[issue] = record
        data["updated_at"] = _now_text()
        _atomic_write_json(compact_path, data)
        _append_jsonl(event_path, {"event": "claimed", "recorded_at": _now_text(), **record})
        return {"claimed": True, **record}


def complete_lease(*, issue: str, lease_id: str, moved_to: str, ledger_dir: str | Path = DEFAULT_LEDGER_DIR) -> None:
    ledger_path = Path(ledger_dir)
    lock_path, compact_path, event_path = _lease_paths(ledger_path)
    with FileLock(lock_path):
        data = _read_json(compact_path, {"schema": "team_os.card_leases.v1", "leases": {}})
        record = data.setdefault("leases", {}).get(issue)
        if record and record.get("lease_id") == lease_id:
            record["status"] = "moved"
            record["moved_to"] = moved_to
            record["completed_at"] = _now_text()
            data["updated_at"] = _now_text()
            _atomic_write_json(compact_path, data)
            _append_jsonl(event_path, {"event": "moved", "recorded_at": _now_text(), **record})


def _default_runner(argv: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, shell=False, timeout=60)
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"command exited {proc.returncode}: {output}")
    return output


def _load_restricted_writer():
    spec = importlib.util.spec_from_file_location("restricted_linear_writer", RESTRICTED_WRITER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load restricted writer: {RESTRICTED_WRITER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _move_with_gate(action: dict[str, Any], *, ledger_dir: Path, runner: Runner) -> dict[str, Any]:
    restricted = _load_restricted_writer()
    return restricted.execute_proposal(action, runner=runner, ledger_dir=ledger_dir)


def _parse_issue_ids(raw: str, *, lane: str | None = None) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            ids = []
            for item in parsed:
                if isinstance(item, dict):
                    if lane and str(item.get("state", item.get("lane", ""))) not in {lane, ""}:
                        continue
                    value = item.get("identifier") or item.get("id")
                else:
                    value = item
                if value:
                    ids.append(str(value))
            return ids
        if isinstance(parsed, dict):
            nodes = parsed.get("nodes") or parsed.get("issues") or parsed.get("data") or []
            if isinstance(nodes, list):
                ids = []
                for item in nodes:
                    if not isinstance(item, dict):
                        continue
                    state = item.get("state")
                    state_name = state.get("name") if isinstance(state, dict) else state
                    if lane and state_name and state_name != lane:
                        continue
                    value = item.get("identifier") or item.get("id")
                    if value:
                        ids.append(str(value))
                return ids
    except json.JSONDecodeError:
        pass
    ids = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = [part.strip() for part in stripped.split("|")]
        token = parts[0].split(maxsplit=1)[0] if parts else ""
        line_lane = parts[1] if len(parts) > 1 else None
        if token.startswith("AGENTS-") and (lane is None or line_lane is None or line_lane == lane):
            ids.append(token)
    return ids


def poll_lane(*, lane: str, project: str = PROJECT_DEFAULT, first: int = 10, runner: Runner = _default_runner) -> list[str]:
    raw = runner([LINEAR_AGENT, "list", "--first", str(first), "--project", project, "--state", lane], None)
    ids = _parse_issue_ids(raw, lane=lane)
    if ids:
        return ids
    # Some helper versions return no rows for custom backlog states even while an
    # unfiltered project listing includes them.  Fallback is read-only and still
    # filters locally before any lease or move.
    raw = runner([LINEAR_AGENT, "list", "--first", str(first), "--project", project], None)
    return _parse_issue_ids(raw, lane=lane)


def _artifact_paths(ledger_dir: Path) -> tuple[Path, Path]:
    return ledger_dir / "team-os-card-artifacts.json", ledger_dir / "team-os-card-artifacts.jsonl"


def _record_artifacts(issue: str, grounding_doc: dict[str, Any], thin_contract: dict[str, Any], ledger_dir: Path) -> None:
    compact, event_path = _artifact_paths(ledger_dir)
    data = _read_json(compact, {"schema": "team_os.card_artifacts.v1"})
    data[issue] = {
        "grounding_doc": grounding_doc,
        "thin_contract": thin_contract,
        "recorded_at": _now_text(),
    }
    _atomic_write_json(compact, data)
    _append_jsonl(event_path, {"event": "artifacts_recorded", "issue": issue, "recorded_at": _now_text()})


def advance_claimed_card(*, issue: str, role: str, from_lane: str,
                         grounding_doc: dict[str, Any] | None = None,
                         thin_contract: dict[str, Any] | None = None,
                         assignee: str = "", ledger_dir: str | Path = DEFAULT_LEDGER_DIR,
                         runner: Runner = _default_runner) -> dict[str, Any]:
    role = role.lower().strip()
    route = ROUTES.get((role, from_lane))
    if route is None:
        return {"ok": False, "error": f"no Phase-2 route for {role} from {from_lane}"}
    if from_lane == "Triage":
        if not isinstance(grounding_doc, dict) or grounding_doc.get("schema") != "team_os.grounding.v1":
            return {"ok": False, "error": "grounding_doc schema team_os.grounding.v1 required"}
        if not isinstance(thin_contract, dict) or thin_contract.get("schema") != "team_os.thin_contract.v1":
            return {"ok": False, "error": "thin_contract schema team_os.thin_contract.v1 required"}
        if assignee != "cto":
            return {"ok": False, "error": "Triage->Todo requires assignee cto"}
        _record_artifacts(issue, grounding_doc, thin_contract, Path(ledger_dir))

    action: dict[str, Any] = {
        "action": "status",
        "issue": issue,
        "from": from_lane,
        "to": route["to"],
        "by": role,
        "conditions_met": route["conditions"],
    }
    if route.get("assignee"):
        action["assignee"] = assignee or route["assignee"]
    result = _move_with_gate(action, ledger_dir=Path(ledger_dir), runner=runner)
    return {"ok": result.get("ok") is True, "issue": issue, "from": from_lane, "moved_to": route["to"], "writer": result}


def run_lane_watcher(*, role: str, lane: str, project: str = PROJECT_DEFAULT,
                     ledger_dir: str | Path = DEFAULT_LEDGER_DIR, runner: Runner = _default_runner,
                     ttl_seconds: int = 900) -> dict[str, Any]:
    role = role.lower().strip()
    route = ROUTES.get((role, lane))
    if route is None or lane == "Triage":
        return {"ok": False, "error": f"no polling watcher route for {role} from {lane}"}
    issues = poll_lane(lane=lane, project=project, runner=runner)
    for issue in issues:
        claim = claim_card(issue=issue, lane=lane, holder=f"{role}:{lane}", ledger_dir=ledger_dir, ttl_seconds=ttl_seconds)
        if not claim.get("claimed"):
            continue
        moved = advance_claimed_card(issue=issue, role=role, from_lane=lane, ledger_dir=ledger_dir, runner=runner)
        if moved.get("ok"):
            complete_lease(issue=issue, lease_id=claim["lease_id"], moved_to=moved["moved_to"], ledger_dir=ledger_dir)
        return {"ok": moved.get("ok") is True, "role": role, "lane": lane, "claimed": issue, "lease_id": claim["lease_id"], "moved_to": moved.get("moved_to"), "move": moved}
    return {"ok": True, "role": role, "lane": lane, "claimed": None, "message": "no unleased cards"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Team OS Phase 2 lane watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("--role", required=True, choices=["cortex", "cto"])
    watch.add_argument("--lane", required=True, choices=["Backlog", "Todo"])
    watch.add_argument("--project", default=PROJECT_DEFAULT)
    watch.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    triage = sub.add_parser("handoff-triage")
    triage.add_argument("--issue", required=True)
    triage.add_argument("--grounding-file", required=True)
    triage.add_argument("--contract-file", required=True)
    triage.add_argument("--assignee", default="cto")
    triage.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    args = parser.parse_args(argv)

    if args.cmd == "watch":
        result = run_lane_watcher(role=args.role, lane=args.lane, project=args.project, ledger_dir=args.ledger_dir)
    else:
        grounding_doc = json.loads(Path(args.grounding_file).read_text(encoding="utf-8"))
        thin_contract = json.loads(Path(args.contract_file).read_text(encoding="utf-8"))
        result = advance_claimed_card(
            issue=args.issue,
            role="cortex",
            from_lane="Triage",
            grounding_doc=grounding_doc,
            thin_contract=thin_contract,
            assignee=args.assignee,
            ledger_dir=args.ledger_dir,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
