"""Phase 8: confidence tracking + goal decomposer tests.

Strict TDD: these specs are written before the implementation.

Boundaries enforced:
    * No LLM calls — decomposer is deterministic and offline.
    * Decomposed tasks are always dry_run=True.
    * Loop selection gates: low/unknown task_confidence blocks dispatch.
    * None task_confidence (not yet assessed) passes through — backward compat.
    * DB persistence is opt-in; reads are always read-only.
"""

from __future__ import annotations

import json
from argparse import Namespace


# ---------------------------------------------------------------------------
# CandidateTask construction
# ---------------------------------------------------------------------------


def test_candidate_task_construction():
    from hermes_cli.team_os.decomposer import CandidateTask

    task = CandidateTask(
        task_id="AGENTS-75-p1",
        title="Add confidence schema",
        description="Extend LoopTask with task_confidence field",
        confidence="high",
        confidence_reasons=("title and description are clear",),
        prerequisites=(),
        approval_required=False,
        reversibility_category="full-instant",
        reversibility_reason="local file change via git",
        route_hint="claude-max",
        verifier_plan=("Run pytest -x -q", "Verify imports"),
        dry_run=True,
    )

    assert task.confidence == "high"
    assert task.dry_run is True
    assert task.prerequisites == ()
    assert task.route_hint == "claude-max"


def test_candidate_task_invalid_confidence_raises():
    from hermes_cli.team_os.decomposer import CandidateTask
    import pytest

    with pytest.raises(ValueError, match="confidence must be one of"):
        CandidateTask(
            task_id="bad",
            title="t",
            description="d",
            confidence="extreme",  # invalid
            confidence_reasons=("reason",),
            prerequisites=(),
            approval_required=False,
            reversibility_category="full-instant",
            reversibility_reason="r",
            route_hint="claude-max",
            verifier_plan=("v",),
        )


def test_candidate_task_invalid_route_hint_raises():
    from hermes_cli.team_os.decomposer import CandidateTask
    import pytest

    with pytest.raises(ValueError, match="route_hint must be one of"):
        CandidateTask(
            task_id="bad",
            title="t",
            description="d",
            confidence="high",
            confidence_reasons=("reason",),
            prerequisites=(),
            approval_required=False,
            reversibility_category="full-instant",
            reversibility_reason="r",
            route_hint="openai-api",  # invalid
            verifier_plan=("v",),
        )


def test_candidate_task_empty_confidence_reasons_raises():
    from hermes_cli.team_os.decomposer import CandidateTask
    import pytest

    with pytest.raises(ValueError, match="confidence_reasons must be non-empty"):
        CandidateTask(
            task_id="bad",
            title="t",
            description="d",
            confidence="high",
            confidence_reasons=(),  # empty
            prerequisites=(),
            approval_required=False,
            reversibility_category="full-instant",
            reversibility_reason="r",
            route_hint="claude-max",
            verifier_plan=("v",),
        )


def test_candidate_task_empty_verifier_plan_raises():
    from hermes_cli.team_os.decomposer import CandidateTask
    import pytest

    with pytest.raises(ValueError, match="verifier_plan must be non-empty"):
        CandidateTask(
            task_id="bad",
            title="t",
            description="d",
            confidence="high",
            confidence_reasons=("reason",),
            prerequisites=(),
            approval_required=False,
            reversibility_category="full-instant",
            reversibility_reason="r",
            route_hint="claude-max",
            verifier_plan=(),  # empty
        )


def test_candidate_task_to_dict():
    from hermes_cli.team_os.decomposer import CandidateTask

    task = CandidateTask(
        task_id="AGENTS-75-p1",
        title="Add schema",
        description="Extend schema",
        confidence="medium",
        confidence_reasons=("partial description",),
        prerequisites=("AGENTS-75-p0",),
        approval_required=False,
        reversibility_category="full-instant",
        reversibility_reason="git revert",
        route_hint="claude-max",
        verifier_plan=("pytest", "compile check"),
        dry_run=True,
    )
    d = task.to_dict()
    assert d["confidence"] == "medium"
    assert d["prerequisites"] == ["AGENTS-75-p0"]
    assert d["confidence_reasons"] == ["partial description"]
    assert d["verifier_plan"] == ["pytest", "compile check"]
    assert d["dry_run"] is True


# ---------------------------------------------------------------------------
# decompose_goal: single task
# ---------------------------------------------------------------------------


def test_decompose_goal_single_task_returns_one_task():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask with task_confidence field and gate on it.",
        labels=["type:code"],
    )
    assert len(tasks) == 1
    task = tasks[0]
    assert task.confidence in {"high", "medium"}
    assert task.dry_run is True
    assert task.prerequisites == ()


def test_decompose_goal_single_task_confidence_reasons_non_empty():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask schema.",
        labels=["type:code"],
    )
    assert tasks[0].confidence_reasons


def test_decompose_goal_single_task_verifier_plan_non_empty():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask schema.",
        labels=["type:code"],
    )
    assert tasks[0].verifier_plan


def test_decompose_goal_single_task_heavy_labels_route_to_claude_max():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Implement confidence gating",
        goal_body="Add gating logic.",
        labels=["type:code"],
    )
    assert tasks[0].route_hint == "claude-max"


def test_decompose_goal_fetch_body_does_not_match_embedded_etc():
    """Words containing an ambiguity token must not be downgraded by substring match."""
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Fetch user records",
        goal_body="Fetch user records from the Team OS state database.",
        labels=["type:code"],
    )
    assert tasks[0].confidence in {"high", "medium"}
    ambiguous_reasons = [
        reason for reason in tasks[0].confidence_reasons
        if "ambiguous markers" in reason
    ]
    assert ambiguous_reasons == []


def test_decompose_goal_standalone_etc_still_marks_ambiguous():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Build unclear extras",
        goal_body="Build schema, CLI, etc.",
        labels=["type:code"],
    )
    assert tasks[0].confidence == "low"
    assert any("etc" in reason for reason in tasks[0].confidence_reasons)


# ---------------------------------------------------------------------------
# decompose_goal: missing / ambiguous title
# ---------------------------------------------------------------------------


def test_decompose_goal_missing_title_returns_unknown_confidence():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="",  # missing
        goal_body="Some body",
        labels=[],
    )
    assert len(tasks) == 1
    assert tasks[0].confidence == "unknown"
    assert tasks[0].route_hint == "none"


def test_decompose_goal_ambiguous_body_produces_low_or_medium_confidence():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Maybe add confidence tracking",
        goal_body="TBD — unclear what exactly to build.",
        labels=[],
    )
    # Ambiguous signals in body + missing labels -> low or medium
    assert tasks[0].confidence in {"low", "medium"}


def test_decompose_goal_no_false_positive_on_substring():
    """'fetch', 'almighty', 'sketch' should NOT trigger ambiguity."""
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Fetch data from DB, sketch the algorithm, almighty goal.",
        labels=["type:code"],
    )
    # Substrings containing 'etc', 'might', etc. should NOT lower confidence
    assert tasks[0].confidence in {"high", "medium"}


def test_decompose_goal_no_labels_produces_medium_or_low():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask.",
        labels=[],  # no labels
    )
    assert tasks[0].confidence in {"medium", "low"}


def test_decompose_goal_empty_body_no_labels_is_low():
    from hermes_cli.team_os.decomposer import decompose_goal

    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Do the thing",
        goal_body="",
        labels=[],
    )
    assert tasks[0].confidence == "low"
    assert any("too sparse" in reason for reason in tasks[0].confidence_reasons)


# ---------------------------------------------------------------------------
# decompose_goal: multi-step body
# ---------------------------------------------------------------------------


def test_decompose_goal_multi_step_body_produces_multiple_tasks():
    from hermes_cli.team_os.decomposer import decompose_goal

    body = (
        "Phase 1: extend schema with task_confidence\n"
        "Phase 2: add DB persistence methods\n"
        "Phase 3: gate loop_runner on confidence\n"
    )
    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Confidence tracking rollout",
        goal_body=body,
        labels=["type:code"],
    )
    assert len(tasks) == 3


def test_decompose_goal_multi_step_prerequisites_chained():
    from hermes_cli.team_os.decomposer import decompose_goal

    body = (
        "Phase 1: extend schema\n"
        "Phase 2: add persistence\n"
    )
    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Rollout",
        goal_body=body,
        labels=["type:code"],
    )
    assert tasks[0].prerequisites == ()
    assert tasks[1].prerequisites == (tasks[0].task_id,)


def test_decompose_goal_max_tasks_caps_output():
    from hermes_cli.team_os.decomposer import decompose_goal

    body = "\n".join(f"Phase {i+1}: step {i+1}" for i in range(10))
    tasks = decompose_goal(
        goal_id="AGENTS-75",
        goal_title="Many steps",
        goal_body=body,
        labels=[],
        max_tasks=4,
    )
    assert len(tasks) == 4


# ---------------------------------------------------------------------------
# LoopTask.task_confidence and skip-reason gating
# ---------------------------------------------------------------------------


def test_loop_task_has_task_confidence_field():
    from hermes_cli.team_os.loop_runner import LoopTask

    task = LoopTask(
        task_id="t1",
        title="task",
        quota_confidence="high",
        task_confidence="high",
    )
    assert task.task_confidence == "high"


def test_loop_task_task_confidence_default_is_none():
    from hermes_cli.team_os.loop_runner import LoopTask

    task = LoopTask(task_id="t1", title="task", quota_confidence="high")
    assert task.task_confidence is None


def test_loop_task_from_dict_reads_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask

    task = LoopTask.from_dict({
        "task_id": "t1",
        "title": "task",
        "quota_confidence": "high",
        "task_confidence": "medium",
    })
    assert task.task_confidence == "medium"


def test_loop_task_from_dict_missing_task_confidence_is_none():
    from hermes_cli.team_os.loop_runner import LoopTask

    task = LoopTask.from_dict({
        "task_id": "t1",
        "title": "task",
        "quota_confidence": "high",
    })
    assert task.task_confidence is None


def test_loop_task_to_dict_includes_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask

    task = LoopTask(task_id="t1", title="task", quota_confidence="high", task_confidence="medium")
    d = task.to_dict()
    assert d["task_confidence"] == "medium"


def test_select_next_blocks_low_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="low-conf",
            title="low confidence task",
            priority=100,
            quota_confidence="high",
            task_confidence="low",
        ),
        LoopTask(
            task_id="high-conf",
            title="high confidence task",
            priority=1,
            quota_confidence="high",
            task_confidence="high",
        ),
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id == "high-conf"
    assert "low-conf" in decision.skipped_task_ids
    assert "task confidence low" in decision.skip_reasons["low-conf"]


def test_select_next_blocks_unknown_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="unknown-conf",
            title="unknown confidence task",
            priority=100,
            quota_confidence="high",
            task_confidence="unknown",
        ),
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id is None
    assert "unknown-conf" in decision.skipped_task_ids
    assert "task confidence unknown" in decision.skip_reasons["unknown-conf"]


def test_select_next_passes_high_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="ready",
            title="ready task",
            quota_confidence="high",
            task_confidence="high",
        ),
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id == "ready"


def test_select_next_passes_medium_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="ready",
            title="ready task",
            quota_confidence="high",
            task_confidence="medium",
        ),
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id == "ready"


def test_select_next_none_task_confidence_passes_through():
    """None means 'not yet assessed by decomposer' — backward compatible pass-through."""
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="legacy",
            title="legacy task (no decomposer)",
            quota_confidence="high",
            task_confidence=None,
        ),
    ]
    decision = select_next_task(tasks, current_shift="day")
    assert decision.selected_task_id == "legacy"


def test_select_next_require_confidence_blocks_none_task_confidence():
    from hermes_cli.team_os.loop_runner import LoopTask, select_next_task

    tasks = [
        LoopTask(
            task_id="legacy",
            title="legacy task (no decomposer)",
            quota_confidence="high",
            task_confidence=None,
        ),
    ]
    decision = select_next_task(tasks, current_shift="day", require_confidence=True)
    assert decision.selected_task_id is None
    assert "legacy" in decision.skipped_task_ids
    assert decision.skip_reasons["legacy"] == "task confidence not assessed"


# ---------------------------------------------------------------------------
# DB: persist_task_confidence / get_task_confidence / list_task_confidence
# ---------------------------------------------------------------------------


def test_db_persist_task_confidence_returns_id(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    db = TeamOSState(tmp_path / "team-os.db")
    row_id = db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="high",
        reasons=["title and description are clear"],
        source="decomposer",
    )
    assert row_id > 0


def test_db_get_task_confidence_reads_back(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    db = TeamOSState(tmp_path / "team-os.db")
    db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="medium",
        reasons=["partial description"],
        source="decomposer",
    )
    record = db.get_task_confidence("AGENTS-75-p1")
    assert record["task_id"] == "AGENTS-75-p1"
    assert record["confidence"] == "medium"
    assert "partial description" in record["reasons"]


def test_db_get_task_confidence_missing_raises_key_error(tmp_path):
    from hermes_cli.team_os.db import TeamOSState
    import pytest

    db = TeamOSState(tmp_path / "team-os.db")
    with pytest.raises(KeyError):
        db.get_task_confidence("nonexistent")


def test_db_list_task_confidence_by_goal(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    db = TeamOSState(tmp_path / "team-os.db")
    db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="high",
        reasons=["clear"],
        source="decomposer",
    )
    db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p2",
        confidence="medium",
        reasons=["sparse"],
        source="decomposer",
    )
    records = db.list_task_confidence("AGENTS-75")
    assert len(records) == 2
    assert {r["task_id"] for r in records} == {"AGENTS-75-p1", "AGENTS-75-p2"}


def test_db_list_task_confidence_different_goal_isolated(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    db = TeamOSState(tmp_path / "team-os.db")
    db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="high",
        reasons=["clear"],
        source="decomposer",
    )
    db.persist_task_confidence(
        goal_id="AGENTS-99",
        task_id="AGENTS-99-p1",
        confidence="low",
        reasons=["ambiguous"],
        source="decomposer",
    )
    records = db.list_task_confidence("AGENTS-75")
    assert all(r["goal_id"] == "AGENTS-75" for r in records)


def test_db_persist_task_confidence_upserts_same_task_id(tmp_path):
    from hermes_cli.team_os.db import TeamOSState

    db = TeamOSState(tmp_path / "team-os.db")
    first_id = db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="medium",
        reasons=["first assessment"],
        source="decomposer",
    )
    second_id = db.persist_task_confidence(
        goal_id="AGENTS-75",
        task_id="AGENTS-75-p1",
        confidence="high",
        reasons=["updated assessment"],
        source="decomposer",
    )

    assert second_id == first_id
    records = db.list_task_confidence("AGENTS-75")
    assert len(records) == 1
    assert records[0]["confidence"] == "high"
    assert records[0]["reasons"] == ["updated assessment"]


# ---------------------------------------------------------------------------
# CLI: decompose-goal command
# ---------------------------------------------------------------------------


def test_cli_decompose_goal_produces_output_file(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "decomposed.json"
    args = Namespace(
        team_os_command="decompose-goal",
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask schema and gate dispatch on confidence.",
        label=["type:code"],
        max_tasks=5,
        output=str(output),
        state_db=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["goal_id"] == "AGENTS-75"
    assert isinstance(data["tasks"], list)
    assert len(data["tasks"]) >= 1


def test_cli_decompose_goal_unknown_title_still_exit_zero(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "decomposed.json"
    args = Namespace(
        team_os_command="decompose-goal",
        goal_id="AGENTS-X",
        goal_title="",
        goal_body="",
        label=[],
        max_tasks=5,
        output=str(output),
        state_db=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["tasks"][0]["confidence"] == "unknown"


def test_cli_decompose_goal_persists_to_state_db(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os
    from hermes_cli.team_os.db import TeamOSState

    db_path = tmp_path / "team-os.db"
    output = tmp_path / "decomposed.json"
    args = Namespace(
        team_os_command="decompose-goal",
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Extend LoopTask schema.",
        label=["type:code"],
        max_tasks=5,
        output=str(output),
        state_db=str(db_path),
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["state_db"] == str(db_path)

    db = TeamOSState(db_path)
    task_id = data["tasks"][0]["task_id"]
    record = db.get_task_confidence(task_id)
    assert record["confidence"] in {"high", "medium", "low", "unknown"}


def test_cli_decompose_goal_all_tasks_dry_run(tmp_path):
    from hermes_cli.team_os.cli import cmd_team_os

    output = tmp_path / "decomposed.json"
    args = Namespace(
        team_os_command="decompose-goal",
        goal_id="AGENTS-75",
        goal_title="Add confidence tracking",
        goal_body="Phase 1: schema\nPhase 2: gating",
        label=["type:code"],
        max_tasks=5,
        output=str(output),
        state_db=None,
    )
    rc = cmd_team_os(args)
    assert rc == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    for task in data["tasks"]:
        assert task["dry_run"] is True
