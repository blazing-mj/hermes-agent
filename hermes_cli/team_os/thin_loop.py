"""Thin Team OS execution loop primitives.

The thin loop is intentionally small: one source ticket, one mission directory,
one isolated git worktree, one Developer slice, one Worker, one Validator gate.
It does not merge, live-dispatch, or mark Linear Done.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DENIED_SURFACES = (
    "prod",
    "production",
    "customer",
    "credential",
    "credentials",
    "secret",
    ".env",
    "gateway/runtime",
    "money",
    "billing",
)


@dataclass(frozen=True)
class MissionPaths:
    mission_dir: Path
    contract_path: Path
    lease_path: Path
    status_path: Path
    worktree_path: Path


def _safe_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "mission"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def build_thin_contract(
    *,
    source_ticket: str,
    title: str,
    allowed_files: list[str],
    required_commands: list[str],
) -> dict[str, Any]:
    """Build the narrow validation contract for one Team OS thin-loop proof."""

    return {
        "role": "planner-output",
        "source_ticket": source_ticket,
        "problem": title,
        "areas": ["Team OS planner-runner acceptance criteria"],
        "files_to_touch": allowed_files,
        "implementation_scope": [
            "Polish planner output acceptance criteria into concrete pass/fail conditions",
            "Reject generic acceptance criteria in the planner validator",
            "Keep changes within the declared file area",
        ],
        "acceptance_criteria": [
            "Planner output acceptance_criteria contain concrete pass/fail conditions tied to observable behavior",
            "Planner validator returns BOUNCE when acceptance_criteria contain generic phrasing such as complete the subtask without observable details",
            "Focused tests prove generic criteria bounce and crisp criteria pass",
        ],
        "proof_required": [
            "Focused pytest output for planner-runner acceptance criteria tests",
            "git diff lines quoted by Validator for every accepted worker claim",
        ],
        "required_commands": required_commands,
        "commands": required_commands,
        "intended_behavior": "AGENTS-172: Planner acceptance criteria are crisp, testable, and Validator-bounced when generic.",
        "non_goals": [
            "Do not merge",
            "Do not live-dispatch",
            "Do not mark Linear Done",
            "Do not change runtime provider configuration",
        ],
        "assertions": [
            "Generic acceptance criteria are detected and bounced",
            "Generated planner contracts use crisp observable acceptance criteria",
            "Only declared planner files/tests are changed",
            "Human gate remains required and auto-Done remains disabled",
        ],
        "behavior_check_required": True,
        "risk": "low",
        "human_gate_required": True,
        "bounce_conditions": [
            "Worker handoff lacks focused proof output",
            "Worker claims are not backed by quoted git diff lines",
            "Changed files escape the allowed file area",
            "Generic acceptance criteria still pass validation",
            "Any merge/live-dispatch/auto-Done attempt appears",
        ],
    }


def check_thin_loop_boundaries(contract: dict[str, Any]) -> list[str]:
    """Return deny reasons before any worktree or agent execution."""

    violations: list[str] = []
    for field in ("files_to_touch", "areas", "implementation_scope", "non_goals"):
        value = contract.get(field, [])
        items = value if isinstance(value, list) else [str(value)]
        for item in items:
            lowered = str(item).lower()
            for denied in _DENIED_SURFACES:
                if denied in lowered:
                    violations.append(f"denied surface in {field}: {item}")
                    break
    if contract.get("human_gate_required") is not True:
        violations.append("human_gate_required must stay true")
    return violations


def prepare_thin_loop_mission(
    *,
    repo_root: Path,
    state_root: Path,
    worktree_root: Path,
    contract: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create mission artifacts and one isolated git worktree, fail-closed."""

    source_ticket = str(contract.get("source_ticket") or "unknown")
    run_id = run_id or str(int(time.time()))
    mission_dir = state_root.expanduser() / source_ticket / run_id
    worktree_path = worktree_root.expanduser() / f"{_safe_slug(source_ticket)}-{_safe_slug(run_id)}"
    paths = MissionPaths(
        mission_dir=mission_dir,
        contract_path=mission_dir / "contract.json",
        lease_path=mission_dir / "lease.json",
        status_path=mission_dir / "status.json",
        worktree_path=worktree_path,
    )

    boundary_violations = check_thin_loop_boundaries(contract)
    if boundary_violations:
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "denied",
                "violations": boundary_violations,
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "denied", "violations": boundary_violations, "paths": _paths_dict(paths)}

    if paths.lease_path.exists():
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "lease_denied",
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "lease_denied", "paths": _paths_dict(paths)}

    _write_json(paths.contract_path, contract)
    _write_json(
        paths.lease_path,
        {
            "owner": "team-os-thin-loop",
            "pid": os.getpid(),
            "source_ticket": source_ticket,
            "run_id": run_id,
            "acquired_at": time.time(),
            "worktree_path": str(worktree_path),
        },
    )
    _write_json(
        paths.status_path,
        {
            "source_ticket": source_ticket,
            "run_id": run_id,
            "status": "prepared",
            "worktree_created": False,
            "auto_done_allowed": False,
            "live_dispatch_allowed": False,
        },
    )

    worktree_root.expanduser().mkdir(parents=True, exist_ok=True)
    branch = f"team-os/{_safe_slug(source_ticket)}-{_safe_slug(run_id)}"
    result = _run_git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo_root.expanduser())
    if result.returncode != 0:
        _write_json(
            paths.status_path,
            {
                "source_ticket": source_ticket,
                "run_id": run_id,
                "status": "worktree_failed",
                "error": result.stderr or result.stdout,
                "worktree_created": False,
                "auto_done_allowed": False,
            },
        )
        return {"ok": False, "status": "worktree_failed", "error": result.stderr or result.stdout, "paths": _paths_dict(paths)}

    _write_json(
        paths.status_path,
        {
            "source_ticket": source_ticket,
            "run_id": run_id,
            "status": "prepared",
            "worktree_created": True,
            "worktree_path": str(worktree_path),
            "branch": branch,
            "auto_done_allowed": False,
            "live_dispatch_allowed": False,
        },
    )
    return {"ok": True, "status": "prepared", "branch": branch, "paths": _paths_dict(paths)}


def build_developer_mission_prompt(*, contract_path: Path, worktree_path: Path, handoff_path: Path) -> str:
    """Render the one-shot prompt for the subscription-only teamos-exec profile."""

    return "\n".join(
        [
            "You are the Team OS Developer slice running under the teamos-exec profile.",
            "Subscription-only execution is required: use native delegate_task, not API keys.",
            "Run exactly one delegate_task Worker. Do not merge, live-dispatch, or mark Linear Done.",
            "Pass the Worker this exact isolated workspace path and require it to edit only allowed files.",
            "Write the final Worker handoff JSON to the handoff path below.",
            "Return compact JSON with ok=true only after the handoff file exists.",
            "",
            f"CONTRACT_PATH: {contract_path}",
            f"WORKTREE_PATH: {worktree_path}",
            f"HANDOFF_PATH: {handoff_path}",
        ]
    )


def run_teamos_exec_slice(
    *,
    contract_path: Path,
    worktree_path: Path,
    handoff_path: Path,
    mission_prompt_path: Path,
    profile: str = "teamos-exec",
    timeout_seconds: float | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Run one Developer slice through ``hermes chat --profile teamos-exec -q``.

    The optional ``command`` exists for focused tests; production callers leave it
    unset so the path uses the Hermes CLI and the configured subscription profile.
    ``timeout_seconds=None`` intentionally avoids reintroducing a fixed 10-minute
    slice cap above the Team OS/gateway deadline layer.
    """

    prompt = build_developer_mission_prompt(
        contract_path=contract_path,
        worktree_path=worktree_path,
        handoff_path=handoff_path,
    )
    mission_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    mission_prompt_path.write_text(prompt + "\n", encoding="utf-8")

    argv = command or [
        "hermes",
        "chat",
        "--profile",
        profile,
        "--toolsets",
        "delegation,file,terminal",
        "-q",
        prompt,
    ]
    completed = subprocess.run(
        argv,
        cwd=worktree_path,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "ok": completed.returncode == 0 and handoff_path.exists(),
        "profile": profile,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "mission_prompt_path": str(mission_prompt_path),
        "handoff_path": str(handoff_path),
    }


def _git_lines(args: list[str], *, cwd: Path) -> list[str]:
    result = _run_git(args, cwd=cwd)
    if result.returncode != 0:
        return [result.stderr or result.stdout]
    return result.stdout.splitlines()


def build_adversarial_validator_prompt(*, contract_path: Path, worktree_path: Path, handoff_path: Path) -> str:
    """Render the cold Claude Max second-opinion Validator prompt."""
    return "\n".join(
        [
            "You are the Team OS adversarial Validator running in a cold Claude Max session.",
            "You must be a different model than the Worker. Worker route is Codex/openai-codex; review route is Claude Max.",
            "Do not re-implement. Do not trust worker claims or substring matches.",
            "Run git diff HEAD in the worktree and verify the diff actually supports each claim semantically.",
            "BOUNCE if a quoted diff substring exists but is unrelated to the claim, too generic, or only proves text presence.",
            "Return strict JSON only: {\"verdict\":\"PASS|BOUNCE\",\"semantic_claims_supported\":true|false,\"model\":\"claude-max\",\"findings\":[...]}.",
            "",
            f"CONTRACT_PATH: {contract_path}",
            f"WORKTREE_PATH: {worktree_path}",
            f"HANDOFF_PATH: {handoff_path}",
        ]
    )


def _extract_review_json(stdout: str) -> dict[str, Any]:
    """Parse direct JSON or Claude Code wrapper JSON whose result field contains JSON."""
    data = json.loads(stdout.strip())
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        try:
            nested = json.loads(data["result"])
        except json.JSONDecodeError:
            return data
        if isinstance(nested, dict):
            return nested
    if not isinstance(data, dict):
        raise ValueError("adversarial review output must be a JSON object")
    return data


def run_adversarial_validator(
    *,
    contract_path: Path,
    worktree_path: Path,
    handoff_path: Path,
    output_path: Path,
    command: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the Claude Max semantic second-opinion rail and persist its review JSON."""
    prompt = build_adversarial_validator_prompt(
        contract_path=contract_path,
        worktree_path=worktree_path,
        handoff_path=handoff_path,
    )
    argv = command or [
        str(Path.home() / ".hermes/bin/claude-max-code"),
        "team-os-adversarial-validator",
        "--",
        "-p",
        prompt,
        "--max-turns",
        "3",
        "--output-format",
        "json",
    ]
    completed = subprocess.run(
        argv,
        cwd=worktree_path,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    review: dict[str, Any] | None = None
    parse_error = None
    if completed.returncode == 0:
        try:
            review = _extract_review_json(completed.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            parse_error = str(exc)
    if review is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "ok": completed.returncode == 0 and review is not None,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "parse_error": parse_error,
        "review": review or {},
        "output_path": str(output_path),
    }


def validate_worker_handoff(
    *,
    contract_path: Path,
    worktree_path: Path,
    handoff_path: Path,
    adversarial_review_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Validate one Worker handoff against the contract and git diff evidence.

    PASS is allowed only when each accepted Worker claim is backed by quoted
    ``git diff HEAD`` lines from the isolated worktree. The Validator never accepts a
    claim solely because the Worker stated it.
    """

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    allowed_files = set(contract.get("files_to_touch", []))
    errors: list[str] = []
    diff_quotes: list[dict[str, Any]] = []
    adversarial_review: dict[str, Any] = {}

    if not handoff_path.exists():
        errors.append("worker handoff is missing")
        handoff: dict[str, Any] = {}
    else:
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))

    changed_files = _git_lines(["diff", "HEAD", "--name-only"], cwd=worktree_path)
    escaped = [path for path in changed_files if path not in allowed_files]
    if escaped:
        errors.append(f"changed files escape allowed area: {escaped}")

    diff_lines = _git_lines(["diff", "HEAD", "--", *sorted(allowed_files)], cwd=worktree_path)
    if not diff_lines:
        errors.append("git diff is empty; no worker claim can be proven")

    if not handoff.get("proof_output"):
        errors.append("worker handoff lacks focused proof output")

    claims = handoff.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("worker handoff must include non-empty claims")
        claims = []

    for idx, claim in enumerate(claims):
        if not isinstance(claim, dict):
            errors.append(f"claim[{idx}] must be an object")
            continue
        text = str(claim.get("claim", "")).strip()
        substrings = claim.get("diff_substrings")
        if not text:
            errors.append(f"claim[{idx}] is missing claim text")
        if not isinstance(substrings, list) or not substrings:
            errors.append(f"claim[{idx}] lacks diff_substrings for Validator proof")
            continue
        claim_quotes: list[str] = []
        for substring in substrings:
            needle = str(substring)
            match = next((line for line in diff_lines if needle in line), None)
            if match is None:
                errors.append(f"claim[{idx}] diff evidence not found: {needle}")
            else:
                claim_quotes.append(match)
        if claim_quotes:
            diff_quotes.append({"claim": text, "diff_lines": claim_quotes})

    if adversarial_review_path is None or not adversarial_review_path.exists():
        errors.append("adversarial semantic review is missing; Claude Max second-opinion PASS required")
    else:
        adversarial_review = json.loads(adversarial_review_path.read_text(encoding="utf-8"))
        if adversarial_review.get("model") != "claude-max":
            errors.append("adversarial review must come from claude-max")
        if adversarial_review.get("verdict") != "PASS":
            errors.append("adversarial semantic review did not PASS")
        if adversarial_review.get("semantic_claims_supported") is not True:
            errors.append("adversarial semantic review says diff does not semantically support every claim")

    verdict = "PASS" if not errors else "BOUNCE"
    result = {
        "verdict": verdict,
        "errors": errors,
        "source_ticket": contract.get("source_ticket"),
        "changed_files": changed_files,
        "diff_quotes": diff_quotes,
        "adversarial_review": adversarial_review,
        "human_gate_required": contract.get("human_gate_required") is True,
        "auto_done_allowed": False,
        "live_dispatch_allowed": False,
    }
    if output_path is not None:
        _write_json(output_path, result)
    return result


def render_proof_ping(*, source_ticket: str, bounce: dict[str, Any], passed: dict[str, Any], commits: list[str]) -> str:
    """Render the post-PASS operator proof ping for Telegram/Linear comments."""

    if passed.get("verdict") != "PASS":
        raise ValueError("proof ping is allowed only after Validator PASS")
    quote_lines = [
        line
        for item in passed.get("diff_quotes", [])
        if isinstance(item, dict)
        for line in item.get("diff_lines", [])
    ]
    return "\n".join(
        [
            f"{source_ticket} thin-loop proof: PASS",
            f"planted_bounce: {bounce.get('verdict')}",
            f"corrected_pass: {passed.get('verdict')}",
            f"changed_files: {', '.join(passed.get('changed_files', []))}",
            f"auto_done_allowed: {str(passed.get('auto_done_allowed')).lower()}",
            "validator_quoted_diff_lines:",
            *(f"- {line}" for line in quote_lines[:8]),
            "commits:",
            *(f"- {commit}" for commit in commits),
        ]
    )


def _paths_dict(paths: MissionPaths) -> dict[str, str]:
    return {
        "mission_dir": str(paths.mission_dir),
        "contract_path": str(paths.contract_path),
        "lease_path": str(paths.lease_path),
        "status_path": str(paths.status_path),
        "worktree_path": str(paths.worktree_path),
    }
