"""Deterministic Team OS Integrator stage.

Phase 1 intent: after the cold Validator PASSes, reversible work can land
without MJ reading code; irreversible actions stop at a plain-language Needs-MJ
gate.  This module is deliberately deterministic/injectable so tests can prove
side-effect boundaries without touching Linear, Telegram, git remotes, or the
live gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Iterable, Sequence

IRREVERSIBLE_TOKENS = frozenset(
    {
        "money",
        "billing",
        "trading",
        "trade",
        "send",
        "sends",
        "email send",
        "klaviyo send",
        "production",
        "prod",
        "customer",
        "credential",
        "credentials",
        "secret",
        "delete",
        "deletes",
        "remove tracked",
        "restart",
        "restarts",
        "daemon restart",
    }
)

RuntimeRunner = Callable[[Sequence[str], Path], str]
LinearCommenter = Callable[[str, str], None]
LinearStatus = Callable[[str, str], None]
FYISender = Callable[[str], None]


@dataclass(frozen=True)
class IntegratorInput:
    source_ticket: str
    worktree_path: Path
    handoff_path: Path
    validator_report_path: Path
    main_branch: str = "main"
    deploy_command: Sequence[str] = field(default_factory=tuple)
    fyi_counter_path: Path | None = None
    fyi_limit: int = 3


@dataclass(frozen=True)
class IntegratorClassification:
    reversibility: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegratorResult:
    status: str
    reversibility: str
    reason: str = ""
    rollback_commands: tuple[str, ...] = ()
    gate_card: str | None = None
    fyi_sent: bool = False
    commands: tuple[tuple[str, ...], ...] = ()


class AutoLandCounter:
    """Small durable counter for Phase-1 training-wheel FYI pings."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_count(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            return 0
        try:
            return int(raw.get("auto_land_fyi_count", 0))
        except (TypeError, ValueError):
            return 0

    def should_send_and_increment(self, *, limit: int) -> bool:
        count = self._read_count()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"auto_land_fyi_count": count + 1}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return count < limit


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _as_texts(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    if isinstance(values, Iterable):
        return [str(v) for v in values]
    return [str(values)]


def _contains_irreversible(text: str) -> str | None:
    haystack = text.casefold()
    for token in sorted(IRREVERSIBLE_TOKENS, key=len, reverse=True):
        token_cf = token.casefold()
        if re.fullmatch(r"[a-z0-9 _-]+", token_cf):
            pattern = r"(?<![a-z0-9])" + re.escape(token_cf).replace(r"\ ", r"[ _-]+") + r"(?![a-z0-9])"
            if re.search(pattern, haystack):
                return token
        elif token_cf in haystack:
            return token
    return None


def classify_integrator_action(handoff: dict[str, Any]) -> IntegratorClassification:
    """Classify whether a post-Validator action may auto-land."""

    reasons: list[str] = []
    risk = str(handoff.get("risk") or "").casefold()
    if risk in {"high", "critical", "irreversible"}:
        reasons.append(f"risk={risk}")

    for field in ("side_effects", "actions", "changed_files", "summary", "title"):
        for text in _as_texts(handoff.get(field)):
            token = _contains_irreversible(text)
            if token:
                reasons.append(f"{field} contains irreversible token '{token}'")
                break

    reversibility = "irreversible" if reasons else "reversible"
    return IntegratorClassification(reversibility=reversibility, reasons=tuple(reasons))


def _plain_language_from_handoff(handoff: dict[str, Any]) -> dict[str, str]:
    raw = handoff.get("plain_language")
    if isinstance(raw, dict):
        return {str(k): str(v).strip() for k, v in raw.items()}
    summary = str(handoff.get("summary") or "Validated work needs approval.").strip()
    return {
        "decision": "Approve or reject the gated action for this ticket.",
        "problem": summary,
        "what_changed": "A validated Worker handoff is ready but contains irreversible risk.",
        "how_it_behaves_now": "Nothing changes until MJ approves the gated action.",
        "approving": "Allow the named irreversible action to proceed.",
        "not_approving": "No money, sends, production/customer action, credential change, delete, or restart is approved beyond this gate.",
        "rollback": "Use the rollback recorded on the ticket to return to the current state.",
        "proof": "Validator verdict and test counts are attached on this Linear issue.",
    }


def _scrub_code(text: str) -> str:
    text = re.sub(r"```.*?```", "[code omitted]", text, flags=re.DOTALL)
    # Hide path/code-ish details from gate cards.  MJ asked for plain language.
    text = re.sub(r"\b[\w.-]+/(?:[\w./-]+)", "[implementation detail omitted]", text)
    return text.strip()


def build_gate_card(
    *,
    problem: str,
    what_changed: str,
    how_it_behaves_now: str,
    approving: str,
    not_approving: str,
    source_ticket: str,
    decision: str | None = None,
    rollback: str | None = None,
    proof: str | None = None,
) -> str:
    """Render the Integrator Needs-MJ card using GATE-CARD-TEMPLATE.md."""

    return "\n".join(
        [
            "## 🛑 What needs your decision",
            _scrub_code(decision or f"Approve or reject the gated action for {source_ticket}."),
            "",
            "## ❓ The problem this solves",
            _scrub_code(problem),
            "",
            "## 🔧 What was changed",
            _scrub_code(what_changed),
            "",
            "## ▶️ How it behaves AFTER you approve",
            _scrub_code(how_it_behaves_now),
            "",
            "## ✅ What you are approving",
            _scrub_code(approving),
            "",
            "## 🚫 What you are NOT approving",
            _scrub_code(not_approving),
            "",
            "## ↩️ If it goes wrong",
            _scrub_code(rollback or "Use the rollback recorded on the ticket to return to the current state."),
            "",
            "## 🔍 Proof it works (for the record, not for you to read)",
            _scrub_code(proof or f"Validator verdict and test counts are attached to {source_ticket}."),
        ]
    ).strip()


def _default_runner(argv: Sequence[str], cwd: Path) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip() or completed.stderr.strip()


def _default_commenter(issue: str, body: str) -> None:
    _default_runner([str(Path.home() / ".hermes/bin/linear-agent"), "comment", issue, body], Path.cwd())


def _default_status(issue: str, status: str) -> None:
    _default_runner([str(Path.home() / ".hermes/bin/linear-agent"), "status", issue, status], Path.cwd())


def _default_fyi(body: str) -> None:
    _default_runner(["hermes", "send", "--to", "telegram", body], Path.cwd())


def _rollback_commands(handoff: dict[str, Any], source_ticket: str, deploy_command: Sequence[str]) -> tuple[str, ...]:
    declared = tuple(_as_texts(handoff.get("rollback_commands")))
    if declared:
        return declared
    # Local-only Integrator rollback must not imply rerunning deploy hooks.  The
    # deploy_command field is retained for backward-compatible input parsing but
    # is not executed by AGENTS-297's canonical Integrator path.
    _ = deploy_command
    return (f"git revert <merge-commit-for-{source_ticket}> --no-edit",)


def _run_auto_land_commands(
    *,
    input_data: IntegratorInput,
    runner: RuntimeRunner,
) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    branch_cmd = ("git", "branch", "--show-current")
    source_branch = runner(branch_cmd, input_data.worktree_path).strip()
    commands.append(branch_cmd)
    if not source_branch:
        raise RuntimeError("cannot auto-land: validated worktree has no current branch")
    if source_branch == input_data.main_branch:
        raise RuntimeError("cannot auto-land: validated worktree is already on the main branch")

    # AGENTS-297: local-only landing is the only safe default.  Do not push to
    # remotes and do not run deploy commands from this Integrator; MJ pushes and
    # deploys manually until a separate, explicitly gated remote path is built.
    for argv in (
        ("git", "fetch", "origin", input_data.main_branch),
        ("git", "checkout", input_data.main_branch),
        ("git", "merge", "--ff-only", source_branch),
    ):
        runner(argv, input_data.worktree_path)
        commands.append(tuple(argv))
    return tuple(commands)


def integrate_after_validator(
    input_data: IntegratorInput,
    *,
    runner: RuntimeRunner = _default_runner,
    linear_commenter: LinearCommenter = _default_commenter,
    linear_status: LinearStatus = _default_status,
    fyi_sender: FYISender = _default_fyi,
) -> IntegratorResult:
    """Apply the deterministic post-Validator decision."""

    handoff = _load_json(input_data.handoff_path)
    validator = _load_json(input_data.validator_report_path)
    verdict = str(validator.get("verdict") or "").upper()
    if verdict != "PASS":
        return IntegratorResult(
            status="blocked",
            reversibility="unknown",
            reason=f"Validator verdict is {verdict or 'missing'}; Integrator only runs after PASS",
        )

    classification = classify_integrator_action(handoff)
    rollback = _rollback_commands(handoff, input_data.source_ticket, input_data.deploy_command)

    if classification.reversibility == "irreversible":
        words = _plain_language_from_handoff(handoff)
        card = build_gate_card(
            problem=words.get("problem", "Approval required."),
            what_changed=words.get("what_changed", "Validated work is ready."),
            how_it_behaves_now=words.get("how_it_behaves_now", "No change until approval."),
            approving=words.get("approving", "Approve the gated irreversible action."),
            not_approving=words.get("not_approving", "No unrelated irreversible action."),
            source_ticket=input_data.source_ticket,
            decision=words.get("decision"),
            rollback=words.get("rollback") or "; ".join(rollback),
            proof=words.get("proof"),
        )
        linear_commenter(input_data.source_ticket, card)
        linear_status(input_data.source_ticket, "Needs-MJ")
        return IntegratorResult(
            status="needs_mj",
            reversibility="irreversible",
            reason="; ".join(classification.reasons),
            rollback_commands=rollback,
            gate_card=card,
        )

    commands = _run_auto_land_commands(input_data=input_data, runner=runner)
    comment = "\n".join(
        [
            f"Integrator auto-landed reversible Validator PASS for {input_data.source_ticket}.",
            "Rollback commands:",
            *(f"- {cmd}" for cmd in rollback),
        ]
    )
    reason = ""
    try:
        linear_commenter(input_data.source_ticket, comment)
    except Exception as exc:  # noqa: BLE001 - rollback must still be returned/emitted.
        reason = f"linear comment failed after auto-land; rollback commands preserved in Integrator result: {exc}"

    fyi_sent = False
    if input_data.fyi_counter_path is not None:
        if AutoLandCounter(input_data.fyi_counter_path).should_send_and_increment(limit=input_data.fyi_limit):
            try:
                fyi_sender(f"FYI: {input_data.source_ticket} reversible Validator PASS landed locally. No remote push or deploy was performed. Rollback is recorded on Linear.")
                fyi_sent = True
            except Exception as exc:  # noqa: BLE001 - FYI must not hide rollback output.
                suffix = f"fyi failed after auto-land; rollback commands preserved in Integrator result: {exc}"
                reason = f"{reason}; {suffix}" if reason else suffix

    return IntegratorResult(
        status="auto_landed",
        reversibility="reversible",
        reason=reason,
        rollback_commands=rollback,
        fyi_sent=fyi_sent,
        commands=commands,
    )
