#!/usr/bin/env python3
"""Restricted Linear writer.

AGENTS-150 Phase A kept unattended writers to list/issue/create/comment only.
AGENTS-190 Phase 1 extends the boundary with *gated* Team OS board moves:

- status moves are allowed only when (from, to, by) appears verbatim in
  docs/team-os/board-transitions.json and all listed conditions are supplied;
- optional assignee changes may occur only as part of that allowed status move;
- every state/config/status side effect is executed through an idempotent outbox;
- standalone assignee mutations and raw GraphQL/stateId payloads remain denied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LINEAR_AGENT = "/Users/alfred/.hermes/bin/linear-agent"
HERMES_HOME = Path.home() / ".hermes"
TRANSITIONS_PATH = HERMES_HOME / "hermes-agent" / "docs" / "team-os" / "board-transitions.json"
DEFAULT_LEDGER_DIR = HERMES_HOME / "state"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
TEAM_KEY = "AGENTS"
ALLOWED_ACTIONS: frozenset[str] = frozenset({"list", "issue", "create", "comment", "status", "ensure_state"})
# Raw state mutations are still denied on non-gated actions. The gated status path
# constructs state changes itself after allowlist validation.
MUTATION_TOKEN_RE = re.compile(r"\b(issueUpdate|stateId|assigneeId|workflowStateCreate)\b", re.IGNORECASE)

SECRET_PATTERNS = [
    re.compile(r"LINEAR_API_KEY\s*=\s*[^\s,;]+", re.IGNORECASE),
    re.compile(r"lin_api_[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"Authorization\s*:\s*Bearer\s+[^\s,;]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
]

Runner = Callable[[list[str], str | None], str]


# ---------------------------------------------------------------------------
# Sanitization / parsing
# ---------------------------------------------------------------------------
def sanitize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("LINEAR_API_KEY"):
            text = pattern.sub("LINEAR_API_KEY=[REDACTED]", text)
        elif pattern.pattern.startswith("Authorization"):
            text = pattern.sub("Authorization: [REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child)
    elif value is not None:
        yield str(value)


def _contains_mutation_token(value: Any) -> str | None:
    for text in _iter_strings(value):
        match = MUTATION_TOKEN_RE.search(text)
        if match:
            return match.group(1)
    return None


def _actions_from(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(proposal.get("actions"), list):
        raw_actions = proposal["actions"]
    else:
        raw_actions = [proposal]
    actions: list[dict[str, Any]] = []
    for raw in raw_actions:
        if isinstance(raw, dict):
            actions.append(raw)
        else:
            actions.append({"action": "", "body": raw})
    return actions


def _load_json(path: str | None) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("proposal must be a JSON object")
    return data


# ---------------------------------------------------------------------------
# Team OS transition gate
# ---------------------------------------------------------------------------
def _load_transition_spec(path: Path = TRANSITIONS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _conditions_from(action: dict[str, Any]) -> set[str]:
    raw = action.get("conditions_met", action.get("conditions", []))
    if raw is True:
        return set(_load_transition_spec().get("conditions_glossary", {}).keys())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(v) for v in raw}


def _validate_status_action(action: dict[str, Any]) -> dict[str, Any]:
    issue = sanitize_text(action.get("issue") or action.get("id") or "").strip()
    from_lane = sanitize_text(action.get("from") or action.get("from_state") or "").strip()
    to_lane = sanitize_text(action.get("to") or action.get("state") or action.get("to_state") or "").strip()
    actor = sanitize_text(action.get("by") or action.get("actor") or "").strip().lower()
    if not issue or not from_lane or not to_lane or not actor:
        raise ValueError("status requires issue, from, to, and by")

    spec = _load_transition_spec()
    lanes = set(spec.get("lanes", []))
    if from_lane not in lanes or to_lane not in lanes:
        raise ValueError(f"status lane not in transition lane allowlist: {from_lane!r}->{to_lane!r}")

    if actor == spec.get("human_override", {}).get("by"):
        allowed = {"from": from_lane, "to": to_lane, "by": [actor], "conditions": [], "human_override": True}
    else:
        allowed = next(
            (
                t for t in spec.get("transitions", [])
                if t.get("from") == from_lane and t.get("to") == to_lane and actor in t.get("by", [])
            ),
            None,
        )
        if not allowed:
            raise ValueError(f"transition rejected by allowlist: {from_lane}->{to_lane} by {actor}")
        required = set(allowed.get("conditions", []))
        supplied = _conditions_from(action)
        missing = sorted(required - supplied)
        if missing:
            raise ValueError(f"transition rejected: missing conditions {missing}")

    assignee = sanitize_text(action.get("assignee") or action.get("assignee_id") or "").strip()
    if assignee and "assignee_set_cto" in allowed.get("conditions", []) and "assignee_set_cto" not in _conditions_from(action):
        raise ValueError("assignee move requires assignee_set_cto condition")
    return {"issue": issue, "from": from_lane, "to": to_lane, "by": actor, "assignee": assignee}


def _validate_ensure_state_action(action: dict[str, Any]) -> dict[str, Any]:
    name = sanitize_text(action.get("name") or action.get("state") or action.get("to") or "").strip()
    actor = sanitize_text(action.get("by") or action.get("actor") or "mj").strip().lower()
    spec = _load_transition_spec()
    if actor not in set(spec.get("roles", [])):
        raise ValueError(f"ensure_state actor not in role allowlist: {actor!r}")
    if name not in set(spec.get("lanes", [])):
        raise ValueError(f"ensure_state rejected: {name!r} not in lane allowlist")
    state_type = sanitize_text(action.get("type") or _default_state_type(name)).strip()
    if state_type not in {"backlog", "unstarted", "started", "completed", "canceled"}:
        raise ValueError(f"ensure_state type rejected: {state_type!r}")
    return {"name": name, "type": state_type, "by": actor}


def _default_state_type(name: str) -> str:
    return {
        "Triage": "unstarted",
        "Backlog": "backlog",
        "Todo": "unstarted",
        "In Progress": "started",
        "In Review": "started",
        "Needs-MJ": "started",
        "Done": "completed",
        "Canceled": "canceled",
    }.get(name, "started")


# ---------------------------------------------------------------------------
# Idempotent outbox
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ledger_paths(ledger_dir: Path) -> tuple[Path, Path]:
    return ledger_dir / "restricted-linear-writer-outbox.jsonl", ledger_dir / "restricted-linear-writer-outbox.json"


def _read_ledger(ledger_dir: Path) -> dict[str, Any]:
    _, compact = _ledger_paths(ledger_dir)
    if not compact.exists():
        return {"operations": {}}
    try:
        data = json.loads(compact.read_text(encoding="utf-8"))
        if isinstance(data.get("operations"), dict):
            return data
    except Exception:
        pass
    return {"operations": {}}


def _write_ledger(ledger_dir: Path, data: dict[str, Any]) -> None:
    _, compact = _ledger_paths(ledger_dir)
    compact.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=compact.parent, prefix=".restricted-linear-writer-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, compact)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_event(ledger_dir: Path, op_id: str, event: str, payload: dict[str, Any]) -> None:
    jsonl, _ = _ledger_paths(ledger_dir)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    row = {"op_id": op_id, "event": event, "recorded_at": _now(), **payload}
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_redact_json(row), sort_keys=True) + "\n")


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_json(v) for k, v in value.items()}
    return value


def _op_id(kind: str, canonical: dict[str, Any]) -> str:
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    if kind == "status":
        return f"team-os-board-move:{canonical['issue']}:{canonical['from']}->{canonical['to']}:{canonical['by']}:{digest}"
    if kind == "ensure_state":
        return f"team-os-board-state:{canonical['name']}:{canonical['type']}:{digest}"
    return f"restricted-linear-writer:{kind}:{digest}"


def _execute_idempotent(kind: str, canonical: dict[str, Any], argv: list[str], stdin: str | None,
                        runner: Runner, ledger_dir: Path) -> str:
    op_id = _op_id(kind, canonical)
    ledger = _read_ledger(ledger_dir)
    existing = ledger.setdefault("operations", {}).get(op_id)
    if existing and existing.get("status") == "local_committed":
        return f"outbox already committed {op_id}"

    now = _now()
    ledger["operations"][op_id] = {
        "op_id": op_id,
        "kind": kind,
        "status": "intent_only",
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
        "canonical": _redact_json(canonical),
        "argv": _redact_json(argv),
    }
    _write_ledger(ledger_dir, ledger)
    _append_event(ledger_dir, op_id, "intent", {"kind": kind, "canonical": canonical})

    output = runner(argv, stdin)

    ledger = _read_ledger(ledger_dir)
    ledger.setdefault("operations", {}).setdefault(op_id, {})
    ledger["operations"][op_id].update({
        "status": "local_committed",
        "updated_at": _now(),
        "external_output": sanitize_text(output).strip()[:2000],
    })
    _write_ledger(ledger_dir, ledger)
    _append_event(ledger_dir, op_id, "local_committed", {"output": sanitize_text(output).strip()[:2000]})
    return f"outbox committed {op_id}: {sanitize_text(output).strip()}"


# ---------------------------------------------------------------------------
# Linear command / GraphQL execution
# ---------------------------------------------------------------------------
def _load_linear_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key:
        return key
    env_path = HERMES_HOME / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("LINEAR_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"\'')
    raise RuntimeError("LINEAR_API_KEY not configured")


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": _load_linear_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Linear GraphQL HTTP {exc.code}: {sanitize_text(body)}") from exc
    if data.get("errors"):
        raise RuntimeError(sanitize_text(json.dumps(data["errors"])))
    return data.get("data", {})


def _linear_issue_update(issue: str, target_state: str, assignee: str = "") -> str:
    # Resolve issue, current state, team states, and users in one bounded read.
    data = _graphql(
        """
        query($issue: String!) {
          issue(id: $issue) { id identifier state { name } team { id key } }
          workflowStates(first: 250) { nodes { id name type team { key } } }
          users(first: 250) { nodes { id name displayName email active } }
        }
        """,
        {"issue": issue},
    )
    item = data.get("issue") or {}
    team_key = ((item.get("team") or {}).get("key")) or TEAM_KEY
    states = [s for s in data.get("workflowStates", {}).get("nodes", []) if (s.get("team") or {}).get("key") == team_key]
    state_id = next((s.get("id") for s in states if s.get("name") == target_state), None)
    if not state_id:
        raise RuntimeError(f"target Linear state not found for {team_key}: {target_state}")
    update: dict[str, Any] = {"stateId": state_id}
    if assignee:
        users = data.get("users", {}).get("nodes", [])
        needle = assignee.lower()
        user_id = next(
            (
                u.get("id") for u in users
                if u.get("id") == assignee
                or str(u.get("email", "")).lower() == needle
                or str(u.get("name", "")).lower() == needle
                or str(u.get("displayName", "")).lower() == needle
            ),
            None,
        )
        if not user_id:
            raise RuntimeError(f"assignee not found: {assignee}")
        update["assigneeId"] = user_id
    result = _graphql(
        "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { identifier state { name } assignee { name email } } } }",
        {"id": issue, "input": update},
    )
    updated = (result.get("issueUpdate") or {}).get("issue") or {}
    return json.dumps(updated, sort_keys=True)


def _ensure_linear_state(name: str, state_type: str) -> str:
    data = _graphql(
        "query { teams { nodes { id key } } workflowStates(first: 250) { nodes { id name type team { key } position } } }"
    )
    team = next((t for t in data.get("teams", {}).get("nodes", []) if t.get("key") == TEAM_KEY), None)
    if not team:
        raise RuntimeError(f"Linear team not found: {TEAM_KEY}")
    existing = next(
        (
            s for s in data.get("workflowStates", {}).get("nodes", [])
            if s.get("name") == name and (s.get("team") or {}).get("key") == TEAM_KEY
        ),
        None,
    )
    if existing:
        return json.dumps({"already_present": True, "workflowState": existing}, sort_keys=True)
    result = _graphql(
        "mutation($input: WorkflowStateCreateInput!) { workflowStateCreate(input: $input) { success workflowState { id name type position team { key } } } }",
        {"input": {"teamId": team["id"], "name": name, "type": state_type, "color": "#f2c94c"}},
    )
    return json.dumps(result.get("workflowStateCreate", {}), sort_keys=True)


def _default_runner(argv: list[str], stdin: str | None = None) -> str:
    if len(argv) >= 4 and argv[0] == LINEAR_AGENT and argv[1] == "status":
        issue = argv[2]
        target = argv[3]
        assignee = ""
        if "--assignee" in argv:
            idx = argv.index("--assignee")
            assignee = argv[idx + 1]
        if assignee:
            return _linear_issue_update(issue, target, assignee)
        # Keep the existing helper path for plain state moves.
    if len(argv) >= 4 and argv[0] == LINEAR_AGENT and argv[1] == "ensure-state":
        return _ensure_linear_state(argv[2], argv[3])

    proc = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        shell=False,
        timeout=60,
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"linear-agent exited {proc.returncode}: {sanitize_text(output)}")
    return output


def _build_argv(action: dict[str, Any]) -> tuple[list[str], str | None, dict[str, Any] | None]:
    kind = str(action.get("action") or action.get("type") or "").strip().lower()

    if kind == "comment":
        issue = sanitize_text(action.get("issue") or action.get("id") or "").strip()
        body = sanitize_text(action.get("body") or action.get("comment") or "")
        if not issue or not body:
            raise ValueError("comment requires issue and body")
        return [LINEAR_AGENT, "comment", issue, "-"], body, None

    if kind == "create":
        title = sanitize_text(action.get("title") or "").strip()
        if not title:
            raise ValueError("create requires title")
        argv = [LINEAR_AGENT, "create", title]
        description = sanitize_text(action.get("description") or action.get("body") or "")
        if description:
            argv.extend(["--description", description])
        project = sanitize_text(action.get("project") or "").strip()
        if project:
            argv.extend(["--project", project])
        priority = action.get("priority")
        if priority is not None:
            argv.extend(["--priority", str(int(priority))])
        labels = action.get("labels") or action.get("label") or []
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            clean = sanitize_text(label).strip()
            if clean:
                argv.extend(["--label", clean])
        return argv, None, None

    if kind == "list":
        argv = [LINEAR_AGENT, "list"]
        first = action.get("first")
        if first is not None:
            argv.extend(["--first", str(int(first))])
        project = sanitize_text(action.get("project") or "").strip()
        if project:
            argv.extend(["--project", project])
        state = sanitize_text(action.get("state") or "").strip()
        if state:
            argv.extend(["--state", state])
        return argv, None, None

    if kind == "issue":
        issue = sanitize_text(action.get("issue") or action.get("id") or "").strip()
        if not issue:
            raise ValueError("issue requires issue/id")
        return [LINEAR_AGENT, "issue", issue], None, None

    if kind == "status":
        gated = _validate_status_action(action)
        argv = [LINEAR_AGENT, "status", gated["issue"], gated["to"]]
        if gated["assignee"]:
            argv.extend(["--assignee", gated["assignee"]])
        return argv, None, gated

    if kind == "ensure_state":
        gated = _validate_ensure_state_action(action)
        return [LINEAR_AGENT, "ensure-state", gated["name"], gated["type"]], None, gated

    raise ValueError(f"unsupported action {kind!r}")


def _dry_run_runner(argv: list[str], stdin: str | None = None) -> str:
    record = {"cmd": argv}
    if stdin is not None:
        record["stdin"] = sanitize_text(stdin)
    return json.dumps(record)


def execute_proposal(
    proposal: dict[str, Any],
    *,
    runner: Runner = _default_runner,
    ledger_dir: str | Path = DEFAULT_LEDGER_DIR,
) -> dict[str, Any]:
    messages: list[str] = []
    executed = 0
    denied = 0
    ledger_path = Path(ledger_dir)

    for action in _actions_from(proposal):
        # The unattended classifier prompt historically says “actions” and lists
        # verbs, but LLMs sometimes emit {"type":"comment"} instead of
        # {"action":"comment"}. Treat that as a surface-schema alias only;
        # all allowlist and payload validation below still applies unchanged.
        kind = str(action.get("action") or action.get("type") or "").strip().lower()
        if kind not in ALLOWED_ACTIONS:
            denied += 1
            messages.append(f"denied action {kind!r}: not in allowlist {sorted(ALLOWED_ACTIONS)}")
            continue

        if kind not in {"status", "ensure_state"}:
            mutation_token = _contains_mutation_token(action)
            if mutation_token:
                denied += 1
                messages.append(f"denied action {kind!r}: mutation token {mutation_token!r} present")
                continue

        try:
            argv, stdin, gated = _build_argv(action)
            if kind in {"status", "ensure_state"} and gated is not None:
                output = _execute_idempotent(kind, gated, argv, stdin, runner, ledger_path)
            else:
                output = runner(argv, stdin)
        except Exception as exc:  # noqa: BLE001 - CLI boundary returns structured failure
            denied += 1
            messages.append(f"error action {kind!r}: {sanitize_text(exc)}")
            continue

        executed += 1
        if output:
            messages.append(sanitize_text(output).strip())

    return {
        "ok": denied == 0,
        "executed": executed,
        "denied": denied,
        "messages": messages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restricted Linear writer (AGENTS-150/190)")
    parser.add_argument("--proposal-file", help="Path to proposal JSON; default reads stdin")
    parser.add_argument("--dry-run", action="store_true", help="Print intended linear-agent calls without invoking them")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR), help="Directory for idempotent outbox files")
    args = parser.parse_args(argv)

    try:
        result = execute_proposal(
            _load_json(args.proposal_file),
            runner=_dry_run_runner if args.dry_run else _default_runner,
            ledger_dir=Path(args.ledger_dir),
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns sanitized error
        result = {"ok": False, "executed": 0, "denied": 1, "messages": [sanitize_text(exc)]}

    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
