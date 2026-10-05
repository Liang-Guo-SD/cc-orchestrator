import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.workers.pi import PiAdapter
from cc_orchestrator.session_store import load_session, save_session


def _cfg(bin_name: str = "pi") -> WorkerConfig:
    return WorkerConfig(
        bin=bin_name,
        new_session_flag="--new-session",
        continue_flag="--continue",
        session_id_flag="--session",
    )


async def _stdout_lines(*lines: bytes):
    for line in lines:
        yield line


@pytest.mark.asyncio
async def test_adapter_writes_output_to_file(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = PiAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    fake_proc = MagicMock()
    fake_proc.stdout = _stdout_lines(b"222 passed\n")
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        result = await adapter.run(
            prompt="do something",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=60,
            output_file=output_file,
        )

    assert result.exit_code == 0
    assert "222 passed" in output_file.read_text()


@pytest.mark.asyncio
async def test_adapter_returns_exit_code_127_when_binary_missing(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = PiAdapter(_cfg(bin_name="nonexistent_binary_xyz"), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    result = await adapter.run(
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        output_file=output_file,
    )

    assert result.exit_code == 127
    assert output_file.exists()
    assert "not found" in output_file.read_text().lower() or "error" in output_file.read_text().lower()


@pytest.mark.asyncio
async def test_adapter_returns_timeout_exit_code(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = PiAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    async def _hangs_forever():
        await asyncio.sleep(10)
        yield b"too slow\n"

    fake_proc = MagicMock()
    fake_proc.stdout = _hangs_forever()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=137)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        result = await adapter.run(
            prompt="slow task",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=0.05,
            output_file=output_file,
        )

    assert result.exit_code == 124  # standard timeout exit code
    fake_proc.kill.assert_called_once()


def test_session_continue_falls_back_to_new_when_no_stored_session(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_id = load_session(sessions_dir, agent="pi", project_key="proj-abc")
    assert session_id is None  # no session stored yet


def test_session_save_and_load_roundtrip(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    save_session(sessions_dir, agent="pi", project_key="proj-abc", session_id="sess-123")
    loaded = load_session(sessions_dir, agent="pi", project_key="proj-abc")
    assert loaded == "sess-123"


@pytest.mark.asyncio
async def test_command_prefix_is_inserted_before_extra_flags(tmp_path):
    cfg = WorkerConfig(
        bin="pi",
        new_session_flag="",
        continue_flag="",
        session_id_flag="",
        extra_flags=["--auto", "--format", "json"],
        command_prefix=["run"],
    )
    adapter = PiAdapter(cfg, tmp_path / "runs", tmp_path / "sessions")
    output_file = tmp_path / "output.txt"

    fake_proc = MagicMock()
    fake_proc.stdout = _stdout_lines(b"ok\n")
    fake_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)) as mock_exec:
        await adapter.run(
            prompt="do something",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=10,
            output_file=output_file,
        )

    called_args = mock_exec.call_args.args
    assert called_args[:6] == ("pi", "run", "--auto", "--format", "json", "do something")


import datetime


@pytest.mark.asyncio
async def test_on_started_callback_receives_process(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = PiAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = _stdout_lines(b"ok\n")
    fake_proc.wait = AsyncMock(return_value=0)

    seen_pids = []
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        await adapter.run(
            prompt="do something",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=10,
            output_file=output_file,
            on_started=lambda proc: seen_pids.append(proc.pid),
        )

    assert seen_pids == [12345]


def test_base_adapter_transcript_path_defaults_to_none(tmp_path):
    from cc_orchestrator.workers.base import WorkerAdapter

    class _Minimal(WorkerAdapter):
        async def run(self, prompt, session, working_dir, timeout_s, output_file, on_started=None):
            raise NotImplementedError

        def extraction_patterns(self):
            return {}

    adapter = _Minimal()
    assert adapter.transcript_path(str(tmp_path), datetime.datetime.now(datetime.timezone.utc)) is None


def test_pi_transcript_path_finds_newest_file_after_cutoff(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    working_dir = tmp_path / "myproject"
    working_dir.mkdir()
    abs_working_dir = str(working_dir.resolve())
    mangled = "-" + abs_working_dir.replace("/", "-") + "--"
    session_dir = fake_home / ".pi" / "agent" / "sessions" / mangled
    session_dir.mkdir(parents=True)

    old_file = session_dir / "2020-01-01T00-00-00-000Z_old.jsonl"
    old_file.write_text("{}\n")
    new_file = session_dir / "2026-10-05T12-00-00-000Z_new.jsonl"
    new_file.write_text("{}\n")

    import os
    import time
    now = time.time()
    os.utime(old_file, (now - 1000, now - 1000))
    os.utime(new_file, (now, now))

    adapter = PiAdapter(_cfg(), tmp_path / "runs", tmp_path / "sessions")
    started_after = datetime.datetime.fromtimestamp(now - 10, tz=datetime.timezone.utc)

    found = adapter.transcript_path(str(working_dir), started_after)
    assert found == new_file


def test_pi_transcript_path_returns_none_when_session_dir_missing(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    adapter = PiAdapter(_cfg(), tmp_path / "runs", tmp_path / "sessions")
    started_after = datetime.datetime.now(datetime.timezone.utc)

    found = adapter.transcript_path(str(tmp_path / "no-such-project"), started_after)
    assert found is None
