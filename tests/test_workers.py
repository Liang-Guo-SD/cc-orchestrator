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
async def test_adapter_returns_exit_code_126_when_binary_not_executable(tmp_path):
    not_executable = tmp_path / "not_executable_binary"
    not_executable.write_text("#!/bin/sh\necho hi\n")
    not_executable.chmod(0o644)  # no execute bit

    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = PiAdapter(_cfg(bin_name=str(not_executable)), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    result = await adapter.run(
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        output_file=output_file,
    )

    assert result.exit_code == 126
    assert output_file.exists()
    assert "permission" in output_file.read_text().lower()


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


def _pi_filename(dt: datetime.datetime, suffix: str) -> str:
    """Build a filename matching pi's real convention:
    <YYYY-MM-DDTHH-MM-SS-mmmZ>_<uuid-ish suffix>.jsonl
    """
    return f"{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{dt.microsecond // 1000:03d}Z_{suffix}.jsonl"


def _session_dir_for(fake_home: Path, working_dir: Path) -> Path:
    abs_working_dir = str(working_dir.resolve())
    mangled = "-" + abs_working_dir.replace("/", "-") + "--"
    session_dir = fake_home / ".pi" / "agent" / "sessions" / mangled
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def test_pi_transcript_path_finds_newest_file_after_cutoff(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    working_dir = tmp_path / "myproject"
    working_dir.mkdir()
    session_dir = _session_dir_for(fake_home, working_dir)

    now = datetime.datetime.now(datetime.timezone.utc)
    old_file = session_dir / _pi_filename(now - datetime.timedelta(hours=1), "old")
    old_file.write_text("{}\n")
    new_file = session_dir / _pi_filename(now, "new")
    new_file.write_text("{}\n")

    adapter = PiAdapter(_cfg(), tmp_path / "runs", tmp_path / "sessions")
    started_after = now - datetime.timedelta(seconds=1)

    found = adapter.transcript_path(str(working_dir), started_after)
    assert found == new_file


def test_pi_transcript_path_ignores_stale_sibling_file_touched_at_dispatch_time(tmp_path, monkeypatch):
    """Regression: selection must be based on the filename's own creation
    timestamp, not mtime. A sibling transcript from an EARLIER dispatch
    (filename timestamp before our started_after) must never be selected,
    even if its mtime looks fresh (e.g. a prior run still writing to it, or
    its file simply being touched) at the moment our dispatch starts.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    working_dir = tmp_path / "myproject"
    working_dir.mkdir()
    session_dir = _session_dir_for(fake_home, working_dir)

    now = datetime.datetime.now(datetime.timezone.utc)
    # A previous run's transcript: created well before our dispatch, but
    # its mtime is "now" because it's still being appended to.
    stale_sibling = session_dir / _pi_filename(now - datetime.timedelta(minutes=10), "stale")
    stale_sibling.write_text("{}\n")
    os_touch(stale_sibling, now.timestamp())

    adapter = PiAdapter(_cfg(), tmp_path / "runs", tmp_path / "sessions")
    started_after = now  # our dispatch starts now; stale_sibling predates it

    found = adapter.transcript_path(str(working_dir), started_after)
    assert found is None, "a stale sibling transcript must not be picked just because its mtime is fresh"

    # Once OUR dispatch's own transcript file actually appears, it must win.
    ours = session_dir / _pi_filename(now + datetime.timedelta(seconds=2), "ours")
    ours.write_text("{}\n")
    found = adapter.transcript_path(str(working_dir), started_after)
    assert found == ours


def os_touch(path: Path, mtime: float) -> None:
    import os
    os.utime(path, (mtime, mtime))


def test_pi_transcript_path_returns_none_when_session_dir_missing(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    adapter = PiAdapter(_cfg(), tmp_path / "runs", tmp_path / "sessions")
    started_after = datetime.datetime.now(datetime.timezone.utc)

    found = adapter.transcript_path(str(tmp_path / "no-such-project"), started_after)
    assert found is None
