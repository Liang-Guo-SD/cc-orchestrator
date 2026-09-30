import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from cc_orchestrator.server import _handle_dispatch, _handle_get_output


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
max_retries: 3
workers:
  opencode:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  pi:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    return cfg


def test_dispatch_returns_run_result_dict(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")

    result = _handle_dispatch(
        agent="opencode",
        prompt="echo hello",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )

    assert "run_id" in result
    assert result["agent"] == "opencode"
    assert result["status"] in ("completed", "failed", "low_confidence")
    assert "claims" in result
    assert "verification_targets" in result
    assert "raw_output_path" in result


def test_dispatch_unknown_agent_returns_error(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = _handle_dispatch(
        agent="unknown_agent_xyz",
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )
    assert result["status"] == "failed"
    assert "unknown" in result["summary"].lower()


def test_get_output_returns_last_n_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runs_dir = tmp_path / ".cc-orchestrator" / "runs" / "run-test-001"
    runs_dir.mkdir(parents=True)
    output_file = runs_dir / "output.txt"
    output_file.write_text("\n".join(f"line {i}" for i in range(500)))

    result = _handle_get_output(run_id="run-test-001", lines=10)
    returned_lines = result.strip().splitlines()
    assert len(returned_lines) == 10
    assert returned_lines[-1] == "line 499"
