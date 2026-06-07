"""CLI for Team OS read-only Phase 1 snapshots."""

from __future__ import annotations

import json
import subprocess
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
from .dispatcher import DispatcherConfig, dispatch_outbox_event
from .planner_runner import plan_goal
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
from .kill_switch import KillSwitch, KillSwitchActive
from .quota import quota_status_unknown
from .router import TaskHints, route_task
from .verification_gate import build_verification_plan, run_verification_plan, write_proof_artifact
from .validator_runner import run_validator, write_result as write_validator_result
from .worker_runner import run_worker

DEFAULT_KILL_SWITCH_STATE = "~/.hermes/state/team-os-kill-switch.json"


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


def _select_for_production_gate(tasks, *, current_shift: str):
    """Pick the highest-priority task whose shift+status match.

    Used only by the Phase 9B production CLI path so the production gate sees
    the candidate it would dispatch, rather than having the candidate silently
    filtered upstream (which would convert a denial into rc=0 no-eligible-task).
    """
    open_statuses = {"ready", "pending", "todo", "backlog"}
    candidates = [
        t
        for t in tasks
        if t.status in open_statuses and current_shift in t.shifts
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda task: (-task.priority, task.task_id))
    return candidates[0]


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

    production = bool(getattr(args, "production", False)) or bool(
        getattr(args, "production_mode", False)
    )
    production_audit_arg = getattr(args, "production_audit", None) or getattr(
        args, "audit_path", None
    )
    production_audit = None
    if production:
        from .production_gate import default_production_audit_path

        production_audit = (
            Path(production_audit_arg).expanduser()
            if production_audit_arg
            else default_production_audit_path()
        )

    tasks = load_loop_tasks(task_file)
    kill_switch_state = Path(
        getattr(args, "kill_switch_state", "~/.hermes/state/team-os-kill-switch.json")
    ).expanduser()
    kill_switch = KillSwitch(kill_switch_state)

    # Phase 9B: in production mode, do NOT let select_next_task pre-filter on
    # approval/confidence/quota — those are the gate's job, and pre-filtering
    # would silently turn a denial into rc=0 "no-eligible-task".  We select
    # against shift+status only, then run the production gate explicitly.
    if production:
        candidate = _select_for_production_gate(
            tasks, current_shift=getattr(args, "shift", "day")
        )
        if candidate is None:
            print(
                json.dumps(
                    {
                        "status": "no-eligible-task",
                        "dry_run": False,
                        "mode": "production",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        from .production_gate import check_production_gate, write_production_audit

        gate = check_production_gate(candidate, kill_switch=kill_switch)
        if not gate.passed:
            if production_audit is not None:
                write_production_audit(
                    task_id=candidate.task_id,
                    task_title=candidate.title,
                    owner=owner,
                    approval_status=candidate.approval_status,
                    task_confidence=candidate.task_confidence,
                    quota_confidence=candidate.quota_confidence,
                    workspace=str(ws.root),
                    audit_path=production_audit,
                    decision="denied",
                    violations=list(gate.violations),
                )
            print(
                json.dumps(
                    {
                        "status": "production_gate_denied",
                        "mode": "production",
                        "violations": list(gate.violations),
                        "task_id": candidate.task_id,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        selected_task = candidate
    else:
        decision = select_next_task(
            tasks,
            current_shift=getattr(args, "shift", "day"),
            require_confidence=bool(getattr(args, "require_confidence", False)),
            require_approval=bool(getattr(args, "require_approval", False)),
            kill_switch=kill_switch,
            production_mode=False,
        )
        if decision.selected_task is None:
            print(
                json.dumps(
                    {
                        "status": "no-eligible-task",
                        "dry_run": False,
                        "skip_reasons": decision.skip_reasons,
                        "mode": "sandbox",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        selected_task = decision.selected_task

    try:
        result = run_active_dispatch(
            selected_task,
            workspace=ws,
            worker_command=tuple(worker_cmd),
            heartbeat_path=Path(heartbeat_path).expanduser(),
            lock_path=lock_path,
            owner=owner,
            max_runtime_seconds=float(getattr(args, "max_runtime_seconds", 120.0)),
            heartbeat_stale_seconds=float(getattr(args, "heartbeat_stale_seconds", 15.0)),
            poll_interval=float(getattr(args, "poll_interval", 0.5)),
            kill_switch=kill_switch,
            production_mode=production,
            audit_path=production_audit if production else None,
        )
    except KillSwitchActive as exc:
        print(json.dumps({"status": "halted", "reason": str(exc), "dry_run": False}, indent=2, sort_keys=True))
        return 1

    payload = result.to_dict()
    payload["mode"] = "production" if production else "sandbox"
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if output:
        write_dispatch_result(result, output)
        print(str(output))
    else:
        print(rendered)
    return 0 if result.status == "succeeded" else 1


def cmd_team_os(args) -> int:  # noqa: ANN001
    command = getattr(args, "team_os_command", None) or "snapshot"
    if command == "cortex":
        from .collectors import collect_linear_project  # noqa: PLC0415
        from .cortex import CortexConfig, run_cortex  # noqa: PLC0415

        state_db_arg = getattr(args, "state_db", None) or "~/.hermes/state/team-os-cortex.db"
        state = TeamOSState(Path(state_db_arg).expanduser())
        linear_projects_arg = list(getattr(args, "linear_project", None) or [])

        def _collector():
            observations = []
            for project in linear_projects_arg:
                observations.extend(collect_linear_project(project))
            return observations

        active = bool(getattr(args, "active", False))
        dry_run = not bool(getattr(args, "live_dispatch", False))
        dispatch_fn = None
        if active and not dry_run:
            repo_root = Path(getattr(args, "repo_root", ".")).expanduser().resolve()
            worktree_root = Path(
                getattr(args, "worktree_root", "~/.hermes/worktrees/team-os-workers")
            ).expanduser()
            artifact_root = Path(
                getattr(args, "artifact_root", "~/.hermes/state/team-os-artifacts")
            ).expanduser()
            lease_root = Path(
                getattr(args, "lease_root", "~/.hermes/state/team-os-leases")
            ).expanduser()
            dispatcher_config = DispatcherConfig(
                repo_root=repo_root,
                worktree_root=worktree_root,
                artifact_root=artifact_root,
                lease_root=lease_root,
                worker_timeout_seconds=float(getattr(args, "worker_timeout_seconds", 600.0)),
                telegram_push_enabled=bool(getattr(args, "telegram_push", False)),
                auto_done_low_cost=bool(getattr(args, "auto_done_low_cost", False)),
            )

            worker = None
            validator = None
            if bool(getattr(args, "stub_dispatch_success", False)):

                def _stub_worker(**kwargs):  # noqa: ANN001
                    contract = kwargs["contract"]
                    branch = kwargs["branch"]
                    return {
                        "worker_status": "completed",
                        "source_ticket": contract["source_ticket"],
                        "worktree_path": str(kwargs["worktree_root"] / branch),
                        "changed_files": list(contract.get("files_to_touch", [])),
                        "proof_results": [
                            {
                                "command": "stub-dispatch-success",
                                "exit_code": 0,
                                "stdout": "ok",
                                "stderr": "",
                            }
                        ],
                        "worker_output": "stub dispatch success",
                        "human_gate_required": True,
                        "loop_feed_allowed": False,
                        "auto_dispatch_allowed": False,
                        "auto_done_allowed": False,
                    }

                def _stub_validator(**_kwargs):  # noqa: ANN001
                    return {
                        "verdict": "PASS",
                        "source_ticket": "stub",
                        "review_text": "VERDICT: PASS\nstep_summary: intent=ok scope=ok acceptance=ok implementation=ok proof=ok",
                        "human_gate_required": True,
                        "auto_done_allowed": False,
                    }

                worker = _stub_worker
                validator = _stub_validator

            def _telegram_push(message: str) -> None:
                completed = subprocess.run(
                    ["hermes", "send", "telegram", "--message", message],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout or "telegram send failed").strip())

            def _auto_done(ticket: str) -> None:
                completed = subprocess.run(
                    [str(Path("~/.hermes/bin/linear-agent").expanduser()), "status", ticket, "Done"],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout or "Linear Done failed").strip())

            def _dispatch(event: dict[str, Any]) -> object:
                result = dispatch_outbox_event(
                    event,
                    dispatcher_config,
                    worker=worker,
                    validator=validator,
                    telegram_push=_telegram_push if bool(getattr(args, "telegram_push", False)) else None,
                    auto_done=_auto_done if bool(getattr(args, "auto_done_low_cost", False)) else None,
                )
                if result.get("status") != "validated":
                    raise RuntimeError(json.dumps(result, sort_keys=True))
                return result

            dispatch_fn = _dispatch

        cortex_result = run_cortex(
            state,
            CortexConfig(
                active=active,
                dry_run=dry_run,
                max_dispatch_per_cycle=int(getattr(args, "max_dispatch_per_cycle", 1)),
            ),
            collector=_collector if linear_projects_arg else None,
            gateway_health_probe=(lambda: True),
            dispatch=dispatch_fn,
        )
        rendered = json.dumps(cortex_result.to_dict(), indent=2, sort_keys=True)
        output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
            print(str(output))
        else:
            print(rendered)
        return 0

    if command == "plan-goal":
        goal_id = getattr(args, "goal_id")
        goal_title = getattr(args, "goal_title", "") or ""
        goal_body = getattr(args, "goal_body", "") or ""
        labels = list(getattr(args, "label", None) or [])
        max_tasks = int(getattr(args, "max_tasks", 10) or 10)
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        result = plan_goal(
            goal_id=goal_id,
            goal_title=goal_title,
            goal_body=goal_body,
            labels=labels,
            max_tasks=max_tasks,
        )
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0 if result["planner_review"]["verdict"] == "PASS" else 1

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
        production = bool(getattr(args, "production", False)) or bool(
            getattr(args, "production_mode", False)
        )

        if production and not active:
            print(
                "loop-runner --production requires --active "
                "(production runs against the sandbox-bounded active dispatch path)",
                file=sys.stderr,
            )
            return 2

        if active:
            return _run_loop_runner_active(args, task_file=task_file, output=output, lock_path=lock_path, owner=owner)

        lock = acquire_runner_lock(
            lock_path,
            owner=owner,
            reclaim=True,
            stale_after_seconds=float(getattr(args, "lock_stale_after_seconds", 300.0)),
        )
        try:
            decision = select_next_task(
                load_loop_tasks(task_file),
                current_shift=getattr(args, "shift", "day"),
                require_confidence=bool(getattr(args, "require_confidence", False)),
                require_approval=bool(getattr(args, "require_approval", False)),
                kill_switch=KillSwitch(Path(getattr(args, "kill_switch_state", "~/.hermes/state/team-os-kill-switch.json")).expanduser()),
                production_mode=bool(getattr(args, "production_mode", False)),
            )
            if output:
                write_loop_decision(decision, output)
                print(str(output))
            else:
                print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
        finally:
            lock.release()
        return 0
    if command == "validate-handoff":
        result = run_validator(
            contract_path=Path(getattr(args, "contract")).expanduser(),
            handoff_path=Path(getattr(args, "handoff")).expanduser(),
            state_path=Path(getattr(args, "state", "~/.hermes/state/team-os-validator-bounces.json")).expanduser(),
            review_cmd=tuple(getattr(args, "review_cmd", None) or ()),
        )
        output = Path(getattr(args, "output", "")).expanduser() if getattr(args, "output", None) else None
        write_validator_result(result, output)
        if result.get("verdict") == "PASS":
            return 0
        return 3 if result.get("escalate_mj") else 1

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
        # Keep a defensive fallback for direct cmd_team_os callers that bypass argparse.
        ks_state = getattr(args, "kill_switch_state", None) or DEFAULT_KILL_SWITCH_STATE
        kill_switch = KillSwitch(Path(ks_state))
        report = run_verification_plan(plan, cwd=Path.cwd(), kill_switch=kill_switch)
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
    if command == "kill-switch":
        ks_action = getattr(args, "ks_action", "status") or "status"
        state_file = getattr(args, "state_file", None)
        reason = getattr(args, "reason", None) or ""
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        ks_path = (
            Path(state_file).expanduser()
            if state_file
            else Path("~/.hermes/state/team-os-kill-switch.json").expanduser()
        )
        ks = KillSwitch(ks_path)
        if ks_action == "enable":
            ks.enable(reason=reason)
            result_data: dict = ks.status()
        elif ks_action == "disable":
            ks.disable()
            result_data = ks.status()
        else:  # status
            result_data = ks.status()
        rendered = json.dumps(result_data, indent=2, sort_keys=True)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0

    if command == "render-template":
        from .contracts import render_template  # noqa: PLC0415
        role = getattr(args, "role", None)
        try:
            template = render_template(role)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
            return 1
        rendered = json.dumps(template, indent=2, sort_keys=True)
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0

    if command == "check-contract":
        from .contracts import check_contract  # noqa: PLC0415
        contract_file_str = getattr(args, "contract_file")
        contract_path = Path(contract_file_str).expanduser()
        try:
            data = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": str(exc), "valid": False}, indent=2), file=sys.stderr)
            return 1
        errors = check_contract(data)
        result_data: dict = {"valid": not errors, "errors": errors}
        rendered = json.dumps(result_data, indent=2, sort_keys=True)
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0 if not errors else 1

    if command == "run-worker":
        contract_path = Path(getattr(args, "contract")).expanduser()
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        repo_root = Path(getattr(args, "repo_root")).expanduser()
        worktree_root = Path(getattr(args, "worktree_root")).expanduser()
        lease_path = Path(getattr(args, "lease")).expanduser()
        branch = getattr(args, "branch")
        timeout_seconds = float(getattr(args, "timeout_seconds", 600.0) or 600.0)
        output_path = (
            Path(getattr(args, "output")).expanduser()
            if getattr(args, "output", None)
            else None
        )
        result = run_worker(
            contract=contract,
            repo_root=repo_root,
            worktree_root=worktree_root,
            lease_path=lease_path,
            branch=branch,
            timeout_seconds=timeout_seconds,
        )
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
            print(str(output_path))
        else:
            print(rendered)
        return 0 if result.get("worker_status") == "completed" else 1

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
    cortex = sub.add_parser(
        "cortex",
        help="Stage 4 gated Cortex Linear poll router (dry-run by default; no live dispatch)",
    )
    cortex.add_argument("--linear-project", action="append", default=[])
    cortex.add_argument("--state-db", default="~/.hermes/state/team-os-cortex.db")
    cortex.add_argument("--output", help="Optional JSON output path")
    cortex.add_argument("--active", action="store_true", default=False)
    cortex.add_argument(
        "--live-dispatch",
        action="store_true",
        default=False,
        help="Opt in to deterministic low-failure-cost Worker->Validator dispatch",
    )
    cortex.add_argument("--max-dispatch-per-cycle", type=int, default=1)
    cortex.add_argument("--repo-root", default=".", help="Repository root for ephemeral Worker worktrees")
    cortex.add_argument(
        "--worktree-root",
        default="~/.hermes/worktrees/team-os-workers",
        help="Root directory for ephemeral Worker worktrees",
    )
    cortex.add_argument(
        "--artifact-root",
        default="~/.hermes/state/team-os-artifacts",
        help="Directory for contract/handoff/validator proof artifacts",
    )
    cortex.add_argument(
        "--lease-root",
        default="~/.hermes/state/team-os-leases",
        help="Directory for Worker lease files",
    )
    cortex.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    cortex.add_argument(
        "--telegram-push",
        action="store_true",
        default=False,
        help="After Validator PASS, send a concise Telegram completion ping",
    )
    cortex.add_argument(
        "--auto-done-low-cost",
        action="store_true",
        default=False,
        help="After Validator PASS, mark only low-failure-cost source Linear tickets Done",
    )
    cortex.add_argument(
        "--stub-dispatch-success",
        action="store_true",
        default=False,
        help="Test-only: replace Worker and Validator with deterministic PASS stubs",
    )
    cortex.set_defaults(func=cmd_team_os)

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
    verification_gate.add_argument(
        "--kill-switch-state",
        default=DEFAULT_KILL_SWITCH_STATE,
        help="Kill-switch JSON state file; missing == disabled, corrupt == fail closed",
    )
    verification_gate.set_defaults(func=cmd_team_os)

    loop_runner = sub.add_parser(
        "loop-runner",
        help="Select the next task in dry-run mode without spawning a worker",
    )
    loop_runner.add_argument("--tasks", required=True, help="JSON fixture list of candidate tasks")
    loop_runner.add_argument("--shift", default="day", choices=["day", "night"])
    loop_runner.add_argument(
        "--require-confidence",
        action="store_true",
        help="Strict Phase 8 gate: block tasks without explicit task_confidence",
    )
    loop_runner.add_argument(
        "--require-approval",
        action="store_true",
        help="Strict Phase 9A gate: block tasks without explicit approval_status",
    )
    loop_runner.add_argument(
        "--kill-switch-state",
        default=DEFAULT_KILL_SWITCH_STATE,
        help="Phase 9A kill-switch JSON state file",
    )
    loop_runner.add_argument("--output", help="Optional decision JSON output path")
    loop_runner.add_argument("--lock", default="~/.hermes/state/team-os-loop-runner.lock")
    loop_runner.add_argument(
        "--lock-stale-after-seconds",
        type=float,
        default=300.0,
        help="Phase 9A: reclaim loop-runner lock after this age when holder is gone/stale",
    )
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
    loop_runner.add_argument(
        "--production-mode",
        action="store_true",
        default=False,
        help=(
            "Phase 9: enable production-mode gate (kill-switch disabled + explicit approval "
            "+ high confidence required); writes audit trail when --audit-path is set"
        ),
    )
    loop_runner.add_argument(
        "--audit-path",
        default=None,
        help=(
            "Phase 9: JSONL file path for production audit trail "
            "(written only when --production-mode is set and dispatch succeeds)"
        ),
    )
    loop_runner.add_argument(
        "--production",
        action="store_true",
        default=False,
        help=(
            "Phase 9B (AGENTS-78): opt into production-mode active dispatch.  "
            "Requires --active.  Runs the production gate before lock + dispatch; "
            "on denial, writes an audit row (when --production-audit is set) and "
            "exits rc=2 without acquiring the lock or calling dispatch."
        ),
    )
    loop_runner.add_argument(
        "--production-audit",
        default=None,
        help=(
            "Phase 9B: JSONL file path for production audit trail.  "
            "Both denial rows and successful-dispatch rows are appended here when --production is set."
        ),
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

    validate = sub.add_parser(
        "validate-handoff",
        help=(
            "Stage 2: run a cold Validator review of one Worker handoff against "
            "one validation contract; no dispatch and no auto-Done"
        ),
    )
    validate.add_argument("--contract", required=True, help="Validation contract JSON path")
    validate.add_argument("--handoff", required=True, help="Worker handoff JSON path")
    validate.add_argument(
        "--state",
        default="~/.hermes/state/team-os-validator-bounces.json",
        help="Bounce-count state JSON path",
    )
    validate.add_argument("--output", help="Optional result JSON path")
    validate.add_argument(
        "--review-cmd",
        nargs="+",
        help="Optional cold-review command argv; receives TEAM_OS_VALIDATOR_PROMPT path",
    )
    validate.set_defaults(func=lambda args: sys.exit(cmd_team_os(args)))

    _register_plan_goal(sub)
    _register_decompose_goal(sub)
    _register_kill_switch(sub)
    _register_contracts(sub)
    _register_run_worker(sub)

    parent.set_defaults(func=cmd_team_os)


def _register_plan_goal(sub) -> None:  # noqa: ANN001
    """Register the dry-run Planner-runner subcommand (AGENTS-170)."""
    plan = sub.add_parser(
        "plan-goal",
        help=(
            "Plan one goal into reviewable subtasks + validation contracts; "
            "runs an intent-preservation review without feeding the loop"
        ),
    )
    plan.add_argument("goal_id", help="Linear/Kanban goal identifier, e.g. AGENTS-170")
    plan.add_argument("--goal-title", default="", help="Human-readable goal title")
    plan.add_argument("--goal-body", default="", help="Goal body / description text")
    plan.add_argument(
        "--label",
        action="append",
        default=[],
        help="Goal label for confidence/routing context (repeatable)",
    )
    plan.add_argument("--max-tasks", type=int, default=10, help="Cap on planned tasks (default 10)")
    plan.add_argument("--output", help="Optional JSON output path")
    plan.set_defaults(func=lambda args: sys.exit(cmd_team_os(args)))


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


def _register_kill_switch(sub) -> None:  # noqa: ANN001
    """Register the kill-switch subcommand (Phase 9A)."""
    ks = sub.add_parser(
        "kill-switch",
        help="Phase 9A: enable/disable/status the Team OS kill-switch",
    )
    ks.add_argument(
        "ks_action",
        choices=["enable", "disable", "status"],
        nargs="?",
        default="status",
        help="Action: enable | disable | status (default: status)",
    )
    ks.add_argument(
        "--reason",
        default="",
        help="Human-readable reason recorded when enabling the kill-switch",
    )
    ks.add_argument(
        "--state-file",
        default=None,
        help="Path to the kill-switch JSON state file (default: ~/.hermes/state/team-os-kill-switch.json)",
    )
    ks.add_argument("--output", help="Optional JSON output path")
    ks.set_defaults(func=cmd_team_os)


def _register_contracts(sub) -> None:  # noqa: ANN001
    """Register render-template and check-contract subcommands (Phase 11 AGENTS-137)."""
    render_tpl = sub.add_parser(
        "render-template",
        help="Phase 11: render a deterministic planner/worker/validator handoff template",
    )
    render_tpl.add_argument(
        "role",
        choices=["planner", "worker", "validator"],
        help="Agent role: planner | worker | validator",
    )
    render_tpl.add_argument("--output", help="Optional JSON output path")
    render_tpl.set_defaults(func=lambda args: sys.exit(cmd_team_os(args)))

    check_contract = sub.add_parser(
        "check-contract",
        help="Phase 11: validate a validation contract JSON file against the required schema",
    )
    check_contract.add_argument(
        "contract_file",
        help="Path to the contract JSON file to validate",
    )
    check_contract.add_argument("--output", help="Optional JSON output path")
    check_contract.set_defaults(func=lambda args: sys.exit(cmd_team_os(args)))


def _register_run_worker(sub) -> None:  # noqa: ANN001
    """Register the Stage 3 isolated Worker runner subcommand (AGENTS-177)."""
    worker = sub.add_parser(
        "run-worker",
        help=(
            "Stage 3: run one Team OS Developer/Worker in an isolated git worktree; "
            "human gate stays on and no auto-Done occurs"
        ),
    )
    worker.add_argument("--contract", required=True, help="Validation contract JSON path")
    worker.add_argument("--repo-root", required=True, help="Source git repository root")
    worker.add_argument(
        "--worktree-root",
        required=True,
        help="Directory under which worker worktrees are created",
    )
    worker.add_argument("--lease", required=True, help="Lease JSON path for this one-ticket worker")
    worker.add_argument("--branch", required=True, help="Ephemeral worker branch/worktree name")
    worker.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
        help="Hard worker timeout in seconds",
    )
    worker.add_argument("--output", help="Optional Worker handoff JSON output path")
    worker.set_defaults(func=lambda args: sys.exit(cmd_team_os(args)))
