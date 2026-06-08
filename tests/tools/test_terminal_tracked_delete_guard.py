import json
import subprocess

import tools.terminal_tool as terminal_tool


def _git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('keep')\n", encoding="utf-8")
    scratch = tmp_path / "scratch.tmp"
    scratch.write_text("scratch\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return tracked, scratch


def test_tracked_delete_guard_blocks_rm_of_git_tracked_file(tmp_path):
    tracked, _scratch = _git_repo(tmp_path)

    error = terminal_tool._tracked_file_delete_guard(f"rm {tracked.name}", cwd=str(tmp_path), authorized=False)

    assert error is not None
    assert "Refusing to delete git-tracked file" in error
    assert str(tracked) in error


def test_tracked_delete_guard_blocks_git_rm_of_git_tracked_file(tmp_path):
    tracked, _scratch = _git_repo(tmp_path)

    error = terminal_tool._tracked_file_delete_guard("git rm tracked.py", cwd=str(tmp_path), authorized=False)

    assert error is not None
    assert "git-tracked file" in error
    assert str(tracked) in error


def test_tracked_delete_guard_blocks_git_clean_pathspec_for_tracked_file(tmp_path):
    tracked, _scratch = _git_repo(tmp_path)

    error = terminal_tool._tracked_file_delete_guard("git clean -fd tracked.py", cwd=str(tmp_path), authorized=False)

    assert error is not None
    assert "git-tracked file" in error
    assert str(tracked) in error


def test_tracked_delete_guard_allows_untracked_scratch_file(tmp_path):
    _tracked, scratch = _git_repo(tmp_path)

    error = terminal_tool._tracked_file_delete_guard(f"rm {scratch.name}", cwd=str(tmp_path), authorized=False)

    assert error is None


def test_tracked_delete_guard_allows_authorized_tracked_delete(tmp_path):
    _tracked, _scratch = _git_repo(tmp_path)

    error = terminal_tool._tracked_file_delete_guard("rm tracked.py", cwd=str(tmp_path), authorized=True)

    assert error is None


def test_terminal_tool_blocks_tracked_rm_before_execution(tmp_path, monkeypatch):
    tracked, _scratch = _git_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = json.loads(terminal_tool.terminal_tool(f"rm {tracked.name}", timeout=10))

    assert result["status"] == "blocked"
    assert "Refusing to delete git-tracked file" in result["error"]
    assert tracked.exists()


def test_terminal_guard_does_not_block_python_scratch_cleanup(tmp_path, monkeypatch):
    _tracked, scratch = _git_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = json.loads(terminal_tool.terminal_tool("python -c 'import shutil; shutil.rmtree(\"scratch-dir\", ignore_errors=True)'", timeout=10))

    assert result.get("status") != "blocked"


def test_terminal_guard_logs_exact_blocked_command(tmp_path, monkeypatch):
    tracked, _scratch = _git_repo(tmp_path)
    guard_log = tmp_path / "tracked-delete-guard.jsonl"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("HERMES_TRACKED_DELETE_GUARD_LOG", str(guard_log))

    result = json.loads(terminal_tool.terminal_tool(f"rm {tracked.name}", timeout=10))

    assert result["status"] == "blocked"
    rows = [json.loads(line) for line in guard_log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["operation"] == "rm"
    assert rows[-1]["command"] == f"rm {tracked.name}"
    assert rows[-1]["cwd"] == str(tmp_path.resolve())
    assert rows[-1]["path"] == str(tracked.resolve())
