#!/usr/bin/env python3
"""Read-only EMA child-output ledger observation helper.

Discovers bounded OpenClaw EMA session/artifact candidates and, when explicit
paths are supplied, runs the offline ema_child_output_ledger verifier. It never
mutates OpenClaw state and performs no sends, restarts, or provider/config edits.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

DEFAULT_OPENCLAW_HOME = Path.home() / ".openclaw"


def _load_ledger_module() -> Any:
    script_path = Path(__file__).resolve().parent / "ema_child_output_ledger.py"
    spec = importlib.util.spec_from_file_location("ema_child_output_ledger", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load ledger module at {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sorted_recent(paths: list[Path], limit: int) -> list[str]:
    existing = [path for path in paths if path.exists()]
    existing.sort(key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)
    return [str(path) for path in existing[:limit]]


def discover_candidates(openclaw_home: Path | str = DEFAULT_OPENCLAW_HOME, *, limit: int = 10) -> dict[str, Any]:
    """Return bounded read-only candidates for EMA session JSONL and artifacts."""
    home = Path(openclaw_home).expanduser()
    session_dirs = [home / "agents" / "ema" / "sessions", home / "agents" / "ema" / "sessions-archive"]
    session_paths: list[Path] = []
    for session_dir in session_dirs:
        if session_dir.exists():
            session_paths.extend(
                path
                for path in session_dir.glob("*.jsonl")
                if ".checkpoint." not in path.name and not path.name.endswith(".trajectory.jsonl")
            )

    ema_instance = home / "workspace-agency" / "roles" / "email-strategist" / "instances" / "ema"
    artifact_candidates = [
        ema_instance / "tmp",
        ema_instance / "copy",
        ema_instance / "artifacts",
    ]
    campaigns_dir = ema_instance / "brands"
    if campaigns_dir.exists():
        artifact_candidates.extend(path.parent for path in campaigns_dir.glob("*/*/*.md"))
    artifact_roots = sorted({path for path in artifact_candidates if path.exists() and any(path.glob("*.md"))}, key=str)

    return {
        "ema_sessions": _sorted_recent(session_paths, limit),
        "artifact_roots": [str(path) for path in artifact_roots[:limit]],
        "notes": "read-only bounded discovery under ~/.openclaw; checkpoint JSONL omitted",
    }


def observe(session_jsonl: Path, artifact_root: Path, openclaw_home: Path, limit: int) -> dict[str, Any]:
    ledger_mod = _load_ledger_module()
    ledger = ledger_mod.build_ledger(session_jsonl, artifact_root)
    return {
        "openclaw_discovery": discover_candidates(openclaw_home, limit=limit),
        "session_jsonl": str(session_jsonl),
        "artifact_root": str(artifact_root),
        "ledger": ledger,
        "ledger_observation_status": "ledger_rows_found" if ledger else "no_ledger_rows_found",
        "production_observation_status": "pending_next_normal_live_EMA_test",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover and optionally run the read-only EMA child-output ledger")
    parser.add_argument("--openclaw-home", type=Path, default=DEFAULT_OPENCLAW_HOME)
    parser.add_argument("--session-jsonl", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    if bool(args.session_jsonl) != bool(args.artifact_root):
        parser.error("--session-jsonl and --artifact-root must be supplied together")

    if args.session_jsonl and args.artifact_root:
        payload = observe(args.session_jsonl, args.artifact_root, args.openclaw_home, args.limit)
    else:
        payload = {"openclaw_discovery": discover_candidates(args.openclaw_home, limit=args.limit)}

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
