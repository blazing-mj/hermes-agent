from __future__ import annotations

import json
from pathlib import Path

JOBS_PATH = Path("/Users/alfred/.hermes/cron/jobs.json")
SCRIPT_DIR = Path("/Users/alfred/.hermes/scripts")
TARGET_JOBS = {
    "openclaw-upstream-watch": "openclaw_upstream_watch_safe.sh",
    "vintage-audit-investigator": "vintage_audit_investigator_safe.sh",
    "lifecycle-health-investigator": "lifecycle_health_investigator_safe.sh",
    "cortex-autonomous-triage-ticketing": "cortex_autonomous_triage_ticketing.sh",
}


def _jobs_by_name() -> dict[str, dict]:
    data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    return {job["name"]: job for job in data["jobs"]}


def test_target_cron_jobs_are_script_only_no_agent_routes():
    jobs = _jobs_by_name()

    for name, script in TARGET_JOBS.items():
        job = jobs[name]
        assert job["no_agent"] is True
        assert job["script"] == script
        assert job.get("enabled_toolsets") in (None, [])
        assert not job.get("prompt", "").strip().startswith("You are investigating")
        assert "route actionable anomalies to Linear" not in job.get("prompt", "")


def test_safe_unattended_scripts_use_no_tools_and_restricted_writer():
    for script in TARGET_JOBS.values():
        text = (SCRIPT_DIR / script).read_text(encoding="utf-8")
        assert "--no-tools" in text
        assert "restricted_linear_writer.py" in text
        assert "-t terminal,file,search" not in text
        assert "linear-agent status" not in text
        assert "issueUpdate" not in text
        assert "stateId" not in text


def test_remaining_safe_collectors_do_not_embed_linear_status_or_graphql_mutations():
    collectors = [
        SCRIPT_DIR / "openclaw_upstream_watch_collect.py",
        SCRIPT_DIR / "vintage_audit_investigator_collect.py",
        SCRIPT_DIR / "lifecycle_health_investigator_collect.py",
    ]
    for collector in collectors:
        text = collector.read_text(encoding="utf-8")
        assert "linear-agent), \"status\"" not in text
        assert "linear-agent status" not in text
        assert " status " not in text
        assert "issueUpdate" not in text
        assert "stateId" not in text
        assert "LINEAR_API_KEY" not in text
