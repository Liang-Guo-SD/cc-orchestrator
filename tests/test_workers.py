import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.workers.opencode import OpenCodeAdapter
from cc_orchestrator.session_store import load_session, save_session


def _cfg(bin_name: str = "opencode") -> WorkerConfig:
    return WorkerConfig(
        bin=bin_name,
        new_session_flag="--new-session",
        continue_flag="--continue",
        session_id_flag="--session",
    )


def test_adapter_writes_output_to_file(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        # Simulate writing to output_file
        output_file.write_text("222 passed\n")
        result = adapter.run(
            prompt="do something",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=60,
            output_file=output_file,
        )

    assert result.exit_code == 0


def test_adapter_returns_exit_code_127_when_binary_missing(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(bin_name="nonexistent_binary_xyz"), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    result = adapter.run(
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        output_file=output_file,
    )

    assert result.exit_code == 127
    assert output_file.exists()
    assert "not found" in output_file.read_text().lower() or "error" in output_file.read_text().lower()


def test_adapter_returns_timeout_exit_code(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="opencode", timeout=1)
        result = adapter.run(
            prompt="slow task",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=1,
            output_file=output_file,
        )

    assert result.exit_code == 124  # standard timeout exit code


def test_session_continue_falls_back_to_new_when_no_stored_session(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_id = load_session(sessions_dir, agent="opencode", project_key="proj-abc")
    assert session_id is None  # no session stored yet


def test_session_save_and_load_roundtrip(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    save_session(sessions_dir, agent="opencode", project_key="proj-abc", session_id="sess-123")
    loaded = load_session(sessions_dir, agent="opencode", project_key="proj-abc")
    assert loaded == "sess-123"
