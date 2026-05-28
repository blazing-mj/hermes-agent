def test_schema_accepts_observability_as_first_class_bucket():
    from hermes_cli.team_os.schema import Bucket, Classification, MechanismType

    classification = Classification(
        primary_bucket=Bucket.OBSERVABILITY,
        secondary_buckets=[Bucket.VERIFIER],
        mechanism_type=MechanismType.REPORT_SANITIZER,
        confidence="high",
        source_proof="AGENTS-54",
    )

    data = classification.to_dict()
    assert data["primary_bucket"] == "observability"
    assert data["secondary_buckets"] == ["verifier"]
    assert data["mechanism_type"] == "report_sanitizer"
    assert data["source_proof"] == "AGENTS-54"


def test_noop_bucket_remains_in_v1_taxonomy():
    from hermes_cli.team_os.schema import Bucket

    assert Bucket.NO_OP.value == "no-op"


def test_quota_stub_returns_unknown_without_percentages():
    from hermes_cli.team_os.quota import quota_status_unknown

    status = quota_status_unknown("codex")

    data = status.to_dict()
    assert data["provider"] == "codex"
    assert data["availability"] == "unknown"
    assert data["confidence"] == "unknown"
    assert "percent" not in data
    assert "%" not in data["reason"]


def test_classifier_marks_ambiguous_agents_9_and_10_not_proof():
    from hermes_cli.team_os.classify import classify_observation
    from hermes_cli.team_os.schema import Observation

    observation = Observation(
        source="linear",
        source_id="AGENTS-9",
        title="Investigate default Hermes gateway disappearance and repeated restarts",
        body="background_process_notifications raw Telegram spam; still in progress",
        status="In Progress",
        project="Hermes System",
        labels=["system:hermes"],
        url="https://linear.app/example/AGENTS-9",
    )

    classified = classify_observation(observation)

    assert classified.ambiguous is True
    assert classified.use_as_proof is False
    assert classified.classification.primary_bucket.value == "observability"
    assert classified.classification.source_proof == "AGENTS-9"


def test_classifier_marks_agents_10_ambiguous_not_proof():
    from hermes_cli.team_os.classify import classify_observation
    from hermes_cli.team_os.schema import Observation

    observation = Observation(
        source="linear",
        source_id="AGENTS-10",
        title="Investigate Hermes over-compaction behavior",
        body="semantic recall and context compaction behavior still under investigation",
        status="In Progress",
        project="Hermes System",
        labels=["system:hermes"],
        url="https://linear.app/example/AGENTS-10",
    )

    classified = classify_observation(observation)

    assert classified.ambiguous is True
    assert classified.use_as_proof is False
    assert classified.classification.confidence == "low"
    assert classified.classification.source_proof == "AGENTS-10"


def test_linear_collector_parses_helper_list_output_without_mutation():
    from hermes_cli.team_os.collectors import collect_linear_issues_from_text

    text = "AGENTS-64 | In Progress | Hermes System | Phase 1 Team OS | system:hermes,type:rail | https://linear.app/x\n"

    issues = collect_linear_issues_from_text(text)

    assert len(issues) == 1
    assert issues[0].source == "linear"
    assert issues[0].source_id == "AGENTS-64"
    assert issues[0].status == "In Progress"
    assert issues[0].labels == ["system:hermes", "type:rail"]


def test_kanban_collector_reads_sqlite_without_writing(tmp_path):
    import os
    import sqlite3

    from hermes_cli.team_os.collectors import collect_kanban_tasks_from_db

    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t_123", "Read-only task", "body", None, "ready", 10, "MJ", 1, None, None, "scratch", None, None, None, None),
    )
    conn.commit()
    conn.close()
    before = os.stat(db_path).st_mtime_ns

    observations = collect_kanban_tasks_from_db(db_path, board="hermes-system")

    after = os.stat(db_path).st_mtime_ns
    assert before == after
    assert observations[0].source == "kanban:hermes-system"
    assert observations[0].source_id == "t_123"
    assert observations[0].status == "ready"


def test_state_db_records_snapshot_and_classifications(tmp_path):
    from hermes_cli.team_os.classify import classify_observation
    from hermes_cli.team_os.db import TeamOSState
    from hermes_cli.team_os.schema import Observation

    state = TeamOSState(tmp_path / "team-os.db")
    state.init_schema()
    observation = Observation(
        source="linear",
        source_id="AGENTS-54",
        title="OpenClaw lifecycle activity spike and 3 recent errors",
        body="watchdog grepped full lifecycle JSON rows and produced false positive alert",
        status="Done",
        project="OpenClaw Core",
        labels=["system:openclaw"],
        url="https://linear.app/example/AGENTS-54",
    )

    snapshot_id = state.record_snapshot([classify_observation(observation)])
    rows = state.list_classifications(snapshot_id)

    assert len(rows) == 1
    assert rows[0]["primary_bucket"] == "observability"
    assert rows[0]["mechanism_type"] == "report_sanitizer"
    assert rows[0]["source_proof"] == "AGENTS-54"
