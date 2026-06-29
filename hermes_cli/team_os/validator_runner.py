"""Cold Validator runner for Team OS handoffs.

The runner is deliberately gate-shaped: it reviews one Worker handoff against one
validation contract, records BOUNCE/PASS, and never dispatches work or marks
Linear Done.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .contracts import check_contract

Reviewer = Callable[[str], str]
WorkerFixer = Callable[[dict[str, Any], dict[str, Any], dict[str, Any], int], dict[str, Any]]
_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(PASS|BOUNCE)\b", re.IGNORECASE | re.MULTILINE)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def build_validator_prompt(*, contract: dict[str, Any], handoff: dict[str, Any]) -> str:
    """Build the cold-review prompt with MJ's required five-step order."""
    return "\n".join(
        [
            "You are the independent Team OS Validator. Cold-review the Worker handoff against the validation contract. Do not use tools; judge only the JSON included below.",
            "Human gate stays ON. Gates stay off. Do not feed the loop. Do not dispatch a worker. Do not mark Linear Done.",
            "Use this exact review order and mention each step in your reasoning summary:",
            "1. intent — confirm the handoff preserves the source ticket intent.",
            "2. scope — confirm implementation stays inside declared scope/non-goals.",
            "3. acceptance — confirm every acceptance/assertion requirement is satisfied. THEN, if the contract declares a non-empty definition_of_done, confirm the handoff's proof_results actually DEMONSTRATE it is met (the named test/command ran and passed in the proof). A definition_of_done that is declared but not demonstrated by real proof is the strongest BOUNCE signal there is.",
            "4. implementation — confirm the described implementation matches the contract and has no scope creep.",
            "5. proof — confirm required proof/commands are present and relevant. A proof command that is a placeholder/incomplete marker (not a real runnable check) counts as missing proof.",
            "Return exactly one verdict line: VERDICT: PASS or VERDICT: BOUNCE.",
            "BOUNCE if proof is missing, the declared definition_of_done is not demonstrated by proof, a contract assertion is unverified, scope drift appears, the human gate is off, or auto-Done/dispatch is attempted.",
            "PASS only if all five steps pass.",
            "",
            "VALIDATION_CONTRACT_JSON:",
            json.dumps(contract, indent=2, sort_keys=True),
            "",
            "WORKER_HANDOFF_JSON:",
            json.dumps(handoff, indent=2, sort_keys=True),
            "",
            "Required output shape:",
            "VERDICT: PASS|BOUNCE",
            "step_summary: intent=... scope=... acceptance=... implementation=... proof=...",
        ]
    )


def _default_reviewer(prompt: str, *, review_cmd: Sequence[str] | None = None) -> str:
    if not review_cmd:
        review_cmd = (
            "/Users/alfred/.hermes/bin/claude-max-code",
            "team-os-validator-runner",
            "--",
            "-p",
            prompt,
            "--model",
            "opus",
            "--max-turns",
            "1",
            "--disallowedTools",
            "Bash,Read,Edit,Write,Glob,Grep,LS,Task,TodoWrite,WebFetch,WebSearch",
        )
        return subprocess.run(review_cmd, text=True, capture_output=True, timeout=600, check=False).stdout

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as f:
        f.write(prompt)
        prompt_path = f.name
    env = os.environ.copy()
    env["TEAM_OS_VALIDATOR_PROMPT"] = prompt_path
    try:
        result = subprocess.run(
            list(review_cmd),
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
            env=env,
        )
        return (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    finally:
        try:
            Path(prompt_path).unlink()
        except OSError:
            pass


def _parse_verdict(text: str) -> str:
    candidate = text or ""
    try:
        wrapped = json.loads(candidate)
        if isinstance(wrapped, dict) and isinstance(wrapped.get("result"), str):
            candidate = wrapped["result"]
    except Exception:
        pass
    match = _VERDICT_RE.search(candidate)
    if not match:
        return "BOUNCE"
    return match.group(1).upper()


def _load_bounce_state(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}
    return {}


def _write_bounce_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_validator(
    *,
    contract_path: Path,
    handoff_path: Path,
    state_path: Path,
    reviewer: Reviewer | None = None,
    review_cmd: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run one cold Validator review and return the gate result."""
    contract = _read_json(contract_path)
    handoff = _read_json(handoff_path)
    contract_errors = check_contract(contract)
    prompt = build_validator_prompt(contract=contract, handoff=handoff)

    if contract_errors:
        review_text = "VERDICT: BOUNCE\ncontract schema failed: " + "; ".join(contract_errors)
        verdict = "BOUNCE"
    else:
        review_text = reviewer(prompt) if reviewer else _default_reviewer(prompt, review_cmd=review_cmd)
        verdict = _parse_verdict(review_text)

    source_ticket = str(contract.get("source_ticket") or handoff.get("source_ticket") or "unknown")
    state = _load_bounce_state(state_path)
    if verdict == "BOUNCE":
        bounce_count = state.get(source_ticket, 0) + 1
        state[source_ticket] = bounce_count
    else:
        bounce_count = 0
        state[source_ticket] = 0
    _write_bounce_state(state_path, state)

    escalate = verdict == "BOUNCE" and bounce_count >= 2
    return {
        "verdict": verdict,
        "source_ticket": source_ticket,
        "review_order": ["intent", "scope", "acceptance", "implementation", "proof"],
        "contract_valid": not contract_errors,
        "contract_errors": contract_errors,
        "bounce_count": bounce_count,
        "tripwire": "MJ escalation required at bounce-count >= 2" if escalate else "bounce-count below MJ escalation threshold",
        "escalate_mj": escalate,
        "human_gate_required": True,
        "loop_feed_allowed": False,
        "auto_dispatch_allowed": False,
        "auto_done_allowed": False,
        "gates_off": True,
        "review_text": review_text.strip(),
    }


def run_bounce_loop(
    *,
    contract: dict[str, Any],
    initial_handoff: dict[str, Any],
    state_path: Path,
    reviewer: Reviewer | None = None,
    review_cmd: Sequence[str] | None = None,
    worker_fixer: WorkerFixer | None = None,
    max_bounces: int = 3,
) -> dict[str, Any]:
    """Run the bounded cruel-Validator -> Worker-fix loop.

    The loop is intentionally narrow and auditable: Validator BOUNCE text may be
    fed to a Worker fixer, but only while the bounce count is below
    ``max_bounces``.  It never marks Linear Done, live-dispatches, or disables
    the human gate.
    """

    if max_bounces < 1:
        raise ValueError("max_bounces must be >= 1")

    current_handoff = dict(initial_handoff)
    validator_results: list[dict[str, Any]] = []
    handoffs: list[dict[str, Any]] = [dict(current_handoff)]

    with tempfile.TemporaryDirectory(prefix="team-os-bounce-loop-") as tmp:
        tmp_path = Path(tmp)
        contract_path = tmp_path / "contract.json"
        handoff_path = tmp_path / "handoff.json"
        contract_path.write_text(json.dumps(contract, indent=2, sort_keys=True), encoding="utf-8")

        for attempt in range(1, max_bounces + 1):
            handoff_path.write_text(json.dumps(current_handoff, indent=2, sort_keys=True), encoding="utf-8")
            result = run_validator(
                contract_path=contract_path,
                handoff_path=handoff_path,
                state_path=state_path,
                reviewer=reviewer,
                review_cmd=review_cmd,
            )
            result["attempt"] = attempt
            validator_results.append(result)

            if result["verdict"] == "PASS":
                return {
                    "status": "passed",
                    "attempts": attempt,
                    "max_bounces": max_bounces,
                    "source_ticket": result["source_ticket"],
                    "validator_results": validator_results,
                    "handoffs": handoffs,
                    "escalate_mj": False,
                    "human_gate_required": True,
                    "loop_feed_allowed": False,
                    "auto_dispatch_allowed": False,
                    "auto_done_allowed": False,
                    "gates_off": True,
                }

            if attempt >= max_bounces:
                break
            if worker_fixer is None:
                break
            current_handoff = worker_fixer(contract, current_handoff, result, attempt)
            if not isinstance(current_handoff, dict):
                raise ValueError("worker_fixer must return a handoff dict")
            handoffs.append(dict(current_handoff))

    status = "max_bounces_exceeded" if len(validator_results) >= max_bounces else "bounced_no_worker_fix"
    return {
        "status": status,
        "attempts": len(validator_results),
        "max_bounces": max_bounces,
        "source_ticket": str(contract.get("source_ticket") or initial_handoff.get("source_ticket") or "unknown"),
        "validator_results": validator_results,
        "handoffs": handoffs,
        "escalate_mj": True,
        "human_gate_required": True,
        "loop_feed_allowed": False,
        "auto_dispatch_allowed": False,
        "auto_done_allowed": False,
        "gates_off": True,
        "tripwire": f"MJ escalation required after {len(validator_results)} BOUNCE verdict(s)",
    }


def write_result(result: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(str(output))
    else:
        print(rendered, end="")
