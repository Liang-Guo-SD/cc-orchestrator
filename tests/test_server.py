import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from cc_orchestrator.server import (
    _handle_dispatch,
    _handle_get_output,
    _handle_get_status,
    _handle_get_progress,
)


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
max_retries: 3
workers:
  pi:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
watch_interval_s: 0.01
idle_threshold_s: 300
error_loop_threshold: 3
""")
    return cfg


def _setup_home(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")


async def _wait_until(predicate, timeout=5.0, interval=0.02):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_dispatch_returns_immediately_with_running_status(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    start = time.monotonic()
    result = await _handle_dispatch(
        agent="pi", prompt="sleep 1 && echo done", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert result["status"] == "running"
    assert "run_id" in result


@pytest.mark.asyncio
async def test_run_status_converges_to_completed(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    dispatched = await _handle_dispatch(
        agent="pi", prompt="echo hello", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    run_id = dispatched["run_id"]

    done = await _wait_until(
        lambda: _handle_get_status(run_id)["status"] != "running"
    )
    assert done

    final = _handle_get_status(run_id)
    assert final["status"] in ("completed", "failed", "low_confidence")
    assert "claims" in final
    assert "verification_targets" in final


@pytest.mark.asyncio
async def test_run_status_is_running_before_completion_with_progress_fields(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    dispatched = await _handle_dispatch(
        agent="pi", prompt="sleep 0.3 && echo done", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    run_id = dispatched["run_id"]

    found_running = await _wait_until(
        lambda: (_handle_get_status(run_id) or {}).get("status") == "running"
        and (_handle_get_status(run_id) or {}).get("idle_seconds") is not None
    )
    assert found_running

    await _wait_until(lambda: _handle_get_status(run_id)["status"] != "running")


@pytest.mark.asyncio
async def test_get_progress_adds_summary_while_running(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    dispatched = await _handle_dispatch(
        agent="pi", prompt="sleep 0.3 && echo done", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    run_id = dispatched["run_id"]

    await _wait_until(lambda: _handle_get_status(run_id).get("status") == "running")
    progress = _handle_get_progress(run_id)
    assert progress["status"] == "running"
    assert isinstance(progress["summary"], str) and progress["summary"] != ""

    await _wait_until(lambda: _handle_get_status(run_id)["status"] != "running")


def test_get_status_unknown_run_id_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    result = _handle_get_status("run-does-not-exist")
    assert result["status"] == "failed"
    assert "no run found" in result["summary"].lower()


@pytest.mark.asyncio
async def test_dispatch_unknown_agent_returns_error(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    result = await _handle_dispatch(
        agent="unknown_agent_xyz", prompt="do something", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    assert result["status"] == "failed"
    assert "unknown" in result["summary"].lower()


@pytest.mark.asyncio
async def test_dispatch_rejects_invalid_session_value(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    result = await _handle_dispatch(
        agent="pi", prompt="do something", session="some-stray-uuid",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    assert result["status"] == "failed"
    assert "session" in result["summary"].lower()


@pytest.mark.asyncio
async def test_fast_failing_process_still_produces_terminal_result(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
max_retries: 3
workers:
  pi:
    bin: nonexistent_binary_xyz
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
watch_interval_s: 0.01
""")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg, tmp_path / ".cc-orchestrator" / "config.yaml")

    dispatched = await _handle_dispatch(
        agent="pi", prompt="irrelevant", session="new",
        working_dir=str(tmp_path), timeout_s=10, task_type="implementation",
    )
    run_id = dispatched["run_id"]

    done = await _wait_until(lambda: _handle_get_status(run_id)["status"] != "running")
    assert done
    final = _handle_get_status(run_id)
    assert final["status"] == "failed"
    assert final["claims"]["exit_code"] == 127


@pytest.mark.asyncio
async def test_parallel_dispatches_run_concurrently(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    (tmp_path / ".cc-orchestrator" / "config.yaml").write_text("""
max_retries: 3
workers:
  pi:
    bin: sh
    command_prefix: ["-c"]
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
watch_interval_s: 0.01
""")

    start = time.monotonic()
    results = await asyncio.gather(
        _handle_dispatch(
            agent="pi", prompt="sleep 1 && echo one done", session="new",
            working_dir=str(tmp_path), timeout_s=10, task_type="code_review_light",
        ),
        _handle_dispatch(
            agent="pi", prompt="sleep 1 && echo two done", session="new",
            working_dir=str(tmp_path), timeout_s=10, task_type="code_review_light",
        ),
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5  # both return immediately now, not after the sleeps
    assert all(r["status"] == "running" for r in results)
    assert results[0]["run_id"] != results[1]["run_id"]

    for r in results:
        await _wait_until(lambda rid=r["run_id"]: _handle_get_status(rid)["status"] != "running")


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
