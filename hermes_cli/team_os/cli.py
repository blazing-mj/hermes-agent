"""CLI for Team OS read-only Phase 1 snapshots."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .approvals import build_approval_sample
from .delivery import TelegramApprovalDelivery, build_delivery_from_sample
from .classify import classify_observation
from .collectors import collect_observations
from .db import TeamOSState
from .decomposer import decompose_goal
from .loop_runner import (
    SandboxBoundaryViolation,
    SandboxWorkspace,
    acquire_runner_lock,
    load_loop_tasks,
    run_active_dispatch,
    select_next_task,
    write_dispatch_result,
    write_loop_decision,
)
from .quota import quota_status_unknown
from .router import TaskHints, route_task
from .verification_gate import build_verification_plan, run_verification_plan, write_proof_artifact


def build_snapshot(
    *,
    linear_projects: list[str],
    kanban_boards: list[str],
    quota_providers: list[str],
    limit_per_kanban_board: int | None = None,
) -> dict[str, Any]:
    observations = collect_observations(
        linear_projects=linear_projects,
        kanban_boards=kanban_boards,
        limit_per_kanban_board=limit_per_kanban_board,
    )
    classified = [classify_observation(obs) for obs in observations]
    return {
        "dry_run": True,
        "blocked_capabilities": [
            "hooks",
            "dispatch",
            "auto_done",
            "telegram_push",
            "customer_infra_writes",
        ],
        "schema": {
            "buckets": [
                "verifier",
                "hook",
                "skill",
                "routing",
                "retrieval",
                "Linear",
                "no-op",
                "observability",
            ],
            "fields": [
                "primary_bucket",
                "secondary_buckets",
                "mechanism_type",
                "confidence",
                "source_proof",
            ],
        },
        "observations": [item.to_dict() for item in classified],
        "quota": [quota_status_unknown(provider).to_dict() for provider in quota_providers],
    }


def _codex_probe_from_arg(value: str | None) -> Callable[[], bool] | None:
    """Map the CLI ``--codex-probe`` value to a deterministic probe callable.

    The CLI never reaches the network: callers explicitly select the probe
    outcome.  ``unavailable`` is the default because Codex usage is not
    confirmed.
    """

    normalized = (value or "unavailable").strip().lower()
    if normalized in {"available", "ok", "yes", "true"}:
        return lambda: True
    if normalized in {"unavailable", "no", "false"}:
        return lambda: False
    if normalized in {"error", "raise", "fail"}:
        def _raises() -> bool:
            raise RuntimeError("codex probe explicitly forced to fail by --codex-probe")
        return _raises
    raise SystemExit(f"unknown --codex-probe value: {value!r}")


def _run_loop_runner_active(
    args,
    *,
    task_file: Path,
    output: Path | None,
    lock_path: Path,
    owner: str,
) -> int:  # noqa: ANN001
    sandbox_root = getattr(args, "sandbox_root", None)
    workspace = getattr(args, "workspace", None)
    worker_cmd = getattr(args, "worker_cmd", None)
    heartbeat_path = getattr(args, "heartbeat_path", None)
    if not sandbox_root or not workspace or not worker_cmd or not heartbeat_path:
        print(
            "loop-runner --active requires --sandbox-root, --workspace, "
            "--worker-cmd, and --heartbeat-path",
            file=sys.stderr,
        )
        return 2

    try:
        ws = SandboxWorkspace.create(Path(workspace).expanduser(), allowed_prefix=Path(sandbox_root).expanduser())
    except SandboxBoundaryViolation as exc:
        print(f"sandbox boundary violation: {exc}", file=sys.stderr)
        return 2

    tasks = load_loop_tasks(task_file)
    decision = select_next_task(tasks, current_shift=getattr(args, "shift", "day"))
    if decision.selected_task is None:
        print(
            json.dumps(
                {
                    "status": "no-eligible-task",
                    "dry_run": False,
                    "skip_reasons": decision.skip_reasons,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = run_active_dispatch(
        decision.selected_task,
        workspace=ws,
        worker_command=tuple(worker_cmd),
        heartbeat_path=Path(heartbeat_path).expanduser(),
        lock_path=lock_path,
        owner=owner,
        max_runtime_seconds=float(getattr(args, "max_runtime_seconds", 120.0)),
        heartbeat_stale_seconds=float(getattr(args, "heartbeat_stale_seconds", 15.0)),
        poll_interval=float(getattr(args, "poll_interval", 0.5)),
    )

    rendered = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if output:
        write_dispatch_result(result, output)
        print(str(output))
    else:
        print(rendered)
    return 0 if result.status == "succeeded" else 1


def cmd_team_os(args) -> int:  # noqa: ANN001
    command = getattr(args, "team_os_command", None) or "snapshot"
    if command == "decompose-goal":
        goal_id = getattr(args, "goal_id")
        goal_title = getattr(args, "goal_title", "") or ""
        goal_body = getattr(args, "goal_body", "") or ""
        labels = list(getattr(args, "label", None) or [])
        max_tasks = int(getattr(args, "max_tasks", 10) or 10)
        state_db = (
            Path(getattr(args, "state_db")).expanduser()
            if getattr(args, "state_db", None)
            else None
        )
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )

        tasks = decompose_goal(
            goal_id=goal_id,
            goal_title=goal_title,
            goal_body=goal_body,
            labels=labels,
            max_tasks=max_tasks,
        )

        result: dict = {
            "goal_id": goal_id,
            "goal_title": goal_title,
            "dry_run": True,
            "tasks": [task.to_dict() for task in tasks],
        }

        if state_db:
            db = TeamOSState(state_db)
            for task in tasks:
                db.persist_task_confidence(
                    goal_id=goal_id,
                    task_id=task.task_id,
                    confidence=task.confidence,
                    reasons=list(task.confidence_reasons),
                    source="decomposer",
                )
            result["state_db"] = str(state_db)

        rendered = json.dumps(result, indent=2, sort_keys=True)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0

    if command == "route":
        hints = TaskHints(
            task_id=getattr(args, "task_id"),
            labels=tuple(getattr(args, "label", None) or ()),
            task_type=getattr(args, "task_type", "unknown") or "unknown",
            quota_confidence_codex=getattr(args, "quota_confidence_codex", "unknown") or "unknown",
            quota_confidence_claude_max=getattr(args, "quota_confidence_claude_max", "unknown") or "unknown",
        )
        probe = _codex_probe_from_arg(getattr(args, "codex_probe", None))
        decision = route_task(hints, codex_probe=probe)
        rendered = json.dumps(decision.to_dict(), indent=2, sort_keys=True)
        output_path = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0
    if command == "loop-runner":
        task_file = Path(getattr(args, "tasks")).expanduser()
        output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        lock_path = Path(getattr(args, "lock", "~/.hermes/state/team-os-loop-runner.lock")).expanduser()
        owner = getattr(args, "owner", "team-os-loop-runner")
        active = bool(getattr(args, "active", False))

        if active:
            return _run_loop_runner_active(args, task_file=task_file, output=output, lock_path=lock_path, owner=owner)

        lock = acquire_runner_lock(lock_path, owner=owner)
        try:
            decision = select_next_task(load_loop_tasks(task_file), current_shift=getattr(args, "shift", "day"))
            if output:
                write_loop_decision(decision, output)
                print(str(output))
            else:
                print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
        finally:
            lock.release()
        return 0
    if command == "verification-gate":
        task_id = getattr(args, "task_id")
        changed_files = list(getattr(args, "changed_file", None) or [])
        focused_tests = list(getattr(args, "test", None) or [])
        plan = build_verification_plan(task_id=task_id, changed_files=changed_files, focused_tests=focused_tests)
        output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        if getattr(args, "plan_only", False):
            rendered_plan = json.dumps(plan.to_dict(), indent=2, sort_keys=True)
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered_plan + "\n", encoding="utf-8")
                print(str(output))
            else:
                print(rendered_plan)
            return 0
        report = run_verification_plan(plan, cwd=Path.cwd())
        if output:
            write_proof_artifact(report, output)
            print(str(output))
        else:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if report.can_close else 1
    if command == "deliver-approval":
        sample = build_approval_sample(
            task_id=getattr(args, "task_id"),
            title=getattr(args, "title"),
            action=getattr(args, "action"),
        )
        dry_run = bool(getattr(args, "dry_run", True))
        state_db = (
            Path(getattr(args, "state_db")).expanduser()
            if getattr(args, "state_db", None)
            else None
        )
        if state_db:
            delivery = build_delivery_from_sample(
                sample, TeamOSState(state_db), dry_run=dry_run,
            )
        else:
            delivery = TelegramApprovalDelivery.from_approval_sample(
                sample, approval_id=0, dry_run=True,
            )
        rendered_delivery = json.dumps(delivery.to_dict(), indent=2, sort_keys=True)
        output_delivery = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        if output_delivery:
            output_delivery.parent.mkdir(parents=True, exist_ok=True)
            output_delivery.write_text(rendered_delivery + "\n", encoding="utf-8")
            print(str(output_delivery))
        else:
            print(rendered_delivery)
        return 0
    if command == "approval-sample":
        sample = build_approval_sample(
            task_id=getattr(args, "task_id", "AGENTS-68"),
            title=getattr(args, "title", "Approval sample"),
            action=getattr(args, "action", "run migration"),
        ).to_dict()
        rendered_sample = json.dumps(sample, indent=2, sort_keys=True)
        output_sample = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        if output_sample:
            output_sample.parent.mkdir(parents=True, exist_ok=True)
            output_sample.write_text(rendered_sample + "\n", encoding="utf-8")
            print(str(output_sample))
        else:
            print(rendered_sample)
        return 0
    if command != "snapshot":
        raise SystemExit(f"unknown team-os command: {command}")

    linear_projects = list(getattr(args, "linear_project", None) or [])
    kanban_boards = list(getattr(args, "kanban_board", None) or [])
    quota_providers = list(getattr(args, "quota_provider", None) or ["codex", "claude-max"])
    output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
    state_db = Path(getattr(args, "state_db", "")).expanduser() if getattr(args, "state_db", None) else None

    snapshot = build_snapshot(
        linear_projects=linear_projects,
        kanban_boards=kanban_boards,
        quota_providers=quota_providers,
        limit_per_kanban_board=getattr(args, "limit_per_kanban_board", None),
    )

    if state_db:
        classified = [
            classify_observation(obs)
            for obs in collect_observations(
                linear_projects=linear_projects,
                kanban_boards=kanban_boards,
                limit_per_kanban_board=getattr(args, "limit_per_kanban_board", None),
            )
        ]
        snapshot_id = TeamOSState(state_db).record_snapshot(classified)
        snapshot["state_db"] = str(state_db)
        snapshot["snapshot_id"] = snapshot_id

    rendered = json.dumps(snapshot, indent=2, sort_keys=True)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(str(output))
    else:
        print(rendered)
    return 0


def register_cli(parent) -> None:  # noqa: ANN001
    sub = parent.add_subparsers(dest="team_os_command")
    snapshot = sub.add_parser(
        "snapshot",
        help="Collect read-only Linear/Kanban state and classify it in dry-run mode",
    )
    snapshot.add_argument(
        "--linear-project",
        action="append",
        default=[],
        help="Linear project to collect via read-only linear-agent list (repeatable)",
    )
    snapshot.add_argument(
        "--kanban-board",
        action="append",
        default=[],
        help="Hermes Kanban board to collect read-only (repeatable)",
    )
    snapshot.add_argument(
        "--quota-provider",
        action="append",
        default=[],
        help="Provider name for unknown quota stub output (repeatable)",
    )
    snapshot.add_argument("--limit-per-kanban-board", type=int, default=None)
    snapshot.add_argument("--state-db", help="Optional local Team OS SQLite state DB path")
    snapshot.add_argument("--output", help="Optional JSON output path")
    snapshot.set_defaults(func=cmd_team_os)

    deliver_approval = sub.add_parser(
        "deliver-approval",
        help="Build a Telegram approval delivery payload (data only, no network)",
    )
    deliver_approval.add_argument("--task-id", required=True)
    deliver_approval.add_argument("--title", required=True)
    deliver_approval.add_argument("--action", required=True)
    deliver_approval.add_argument(
        "--state-db",
        default=None,
        help="Optional Team OS SQLite DB to persist the approval request",
    )
    deliver_approval.add_argument("--output", help="Optional JSON output path")
    deliver_approval.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Mark the delivery as dry-run (default).  Production opt-in is explicit.",
    )
    deliver_approval.set_defaults(func=cmd_team_os)

    approval_sample = sub.add_parser(
        "approval-sample",
        help="Render one local approval prompt sample without sending it",
    )
    approval_sample.add_argument("--task-id", default="AGENTS-68")
    approval_sample.add_argument("--title", default="Approval sample")
    approval_sample.add_argument("--action", default="run database migration")
    approval_sample.add_argument("--output", help="Optional JSON output path")
    approval_sample.set_defaults(func=cmd_team_os)

    verification_gate = sub.add_parser(
        "verification-gate",
        help="Run the Phase 3 local verification gate and write proof JSON",
    )
    verification_gate.add_argument("task_id")
    verification_gate.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="Changed file path used for syntax/lint/smoke selection (repeatable)",
    )
    verification_gate.add_argument(
        "--test",
        action="append",
        default=[],
        help="Focused pytest target from the task plan/grounding (repeatable)",
    )
    verification_gate.add_argument("--output", help="Optional proof JSON output path")
    verification_gate.add_argument("--plan-only", action="store_true", help="Write selected commands without running them")
    verification_gate.set_defaults(func=cmd_team_os)

    loop_runner = sub.add_parser(
        "loop-runner",
        help="Select the next task in dry-run mode without spawning a worker",
    )
    loop_runner.add_argument("--tasks", required=True, help="JSON fixture list of candidate tasks")
    loop_runner.add_argument("--shift", default="day", choices=["day", "night"])
    loop_runner.add_argument("--output", help="Optional decision JSON output path")
    loop_runner.add_argument("--lock", default="~/.hermes/state/team-os-loop-runner.lock")
    loop_runner.add_argument("--owner", default="team-os-loop-runner")
    loop_runner.add_argument(
        "--active",
        action="store_true",
        help="Phase 6: actively dispatch the selected task into a sandbox worker",
    )
    loop_runner.add_argument(
        "--sandbox-root",
        help="Phase 6: required prefix every workspace path must live under",
    )
    loop_runner.add_argument(
        "--workspace",
        help="Phase 6: sandbox workspace directory (must be inside --sandbox-root)",
    )
    loop_runner.add_argument(
        "--worker-cmd",
        nargs="+",
        help="Phase 6: argv for the local sandbox worker (no real agent spawn)",
    )
    loop_runner.add_argument(
        "--heartbeat-path",
        help="Phase 6: file path the worker touches to prove liveness",
    )
    loop_runner.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=120.0,
        help="Phase 6: hard upper bound on worker runtime (seconds)",
    )
    loop_runner.add_argument(
        "--heartbeat-stale-seconds",
        type=float,
        default=15.0,
        help="Phase 6: reclaim worker if heartbeat is older than this (seconds)",
    )
    loop_runner.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Phase 6: dispatcher poll interval (seconds)",
    )
    loop_runner.set_defaults(func=cmd_team_os)

    route = sub.add_parser(
        "route",
        help="Decide Codex vs Claude Code Max for a task in dry-run mode",
    )
    route.add_argument("task_id")
    route.add_argument(
        "--label",
        action="append",
        default=[],
        help="Task label such as type:code or type:host (repeatable)",
    )
    route.add_argument("--task-type", default="unknown")
    route.add_argument(
        "--quota-confidence-codex",
        default="unknown",
        help="Codex quota confidence: high|medium|low|unknown|unavailable|exhausted",
    )
    route.add_argument(
        "--quota-confidence-claude-max",
        default="unknown",
        help="Claude-max quota confidence: high|medium|low|unknown|unavailable|exhausted",
    )
    route.add_argument(
        "--codex-probe",
        default="unavailable",
        choices=("available", "unavailable", "error"),
        help="Deterministic codex availability probe outcome (no network)",
    )
    route.add_argument("--output", help="Optional decision JSON output path")
    route.set_defaults(func=cmd_team_os)

    _register_decompose_goal(sub)

    parent.set_defaults(func=cmd_team_os)


def _register_decompose_goal(sub) -> None:  # noqa: ANN001
    """Register the decompose-goal subcommand (called from register_cli)."""
    decompose = sub.add_parser(
        "decompose-goal",
        help="Phase 8: decompose a goal into candidate tasks with confidence scoring",
    )
    decompose.add_argument("goal_id", help="Linear/Kanban goal identifier, e.g. AGENTS-75")
    decompose.add_argument("--goal-title", default="", help="Human-readable goal title")
    decompose.add_argument("--goal-body", default="", help="Goal body / description text")
    decompose.add_argument(
        "--label",
        action="append",
        default=[],
        help="Goal labels such as type:code (repeatable)",
    )
    decompose.add_argument("--max-tasks", type=int, default=10, help="Cap on decomposed tasks (default 10)")
    decompose.add_argument("--state-db", default=None, help="Optional Team OS SQLite DB path for persistence")
    decompose.add_argument("--output", help="Optional JSON output path")
    decompose.set_defaults(func=cmd_team_os)
