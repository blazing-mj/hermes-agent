"""CLI for Team OS read-only Phase 1 snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .approvals import build_approval_sample
from .classify import classify_observation
from .collectors import collect_observations
from .db import TeamOSState
from .loop_runner import acquire_runner_lock, load_loop_tasks, select_next_task, write_loop_decision
from .quota import quota_status_unknown
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


def cmd_team_os(args) -> int:  # noqa: ANN001
    command = getattr(args, "team_os_command", None) or "snapshot"
    if command == "loop-runner":
        task_file = Path(getattr(args, "tasks")).expanduser()
        output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        lock_path = Path(getattr(args, "lock", "~/.hermes/state/team-os-loop-runner.lock")).expanduser()
        owner = getattr(args, "owner", "team-os-loop-runner")
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
    loop_runner.set_defaults(func=cmd_team_os)
    parent.set_defaults(func=cmd_team_os)
