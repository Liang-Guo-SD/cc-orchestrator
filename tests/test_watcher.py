import asyncio
import datetime
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from cc_orchestrator.watcher import (
    parse_new_entries,
    extract_tool_results,
    detect_error_loop,
    touched_files_from_git,
    watch_run,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _tool_result(tool_name: str, is_error: bool, text: str, ts: str) -> dict:
    return {
        "type": "message",
        "timestamp": ts,
        "message": {
            "role": "toolResult",
            "toolCallId": "call_x",
            "toolName": tool_name,
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


def test_parse_new_entries_reads_complete_lines_and_tracks_offset(tmp_path):
    f = tmp_path / "transcript.jsonl"
    _write_jsonl(f, [{"type": "session", "id": "1"}, {"type": "message", "id": "2"}])

    entries, offset = parse_new_entries(f, 0)
    assert len(entries) == 2
    assert offset == f.stat().st_size

    # No new data since last read -> no new entries, offset unchanged
    entries2, offset2 = parse_new_entries(f, offset)
    assert entries2 == []
    assert offset2 == offset


def test_parse_new_entries_holds_back_incomplete_trailing_line(tmp_path):
    f = tmp_path / "transcript.jsonl"
    complete = json.dumps({"type": "message", "id": "1"})
    f.write_text(complete + "\n" + '{"type": "message", "id": "2"')  # truncated, no closing

    entries, offset = parse_new_entries(f, 0)
    assert len(entries) == 1
    assert entries[0]["id"] == "1"
    # offset should point just past the complete line, not consume the partial one
    assert offset == len((complete + "\n").encode("utf-8"))

    # Completing the second line later and re-reading from the held-back offset works
    f.write_text(complete + "\n" + json.dumps({"type": "message", "id": "2"}) + "\n")
    entries2, offset2 = parse_new_entries(f, offset)
    assert len(entries2) == 1
    assert entries2[0]["id"] == "2"


def test_parse_new_entries_missing_file_returns_empty(tmp_path):
    entries, offset = parse_new_entries(tmp_path / "does-not-exist.jsonl", 0)
    assert entries == []
    assert offset == 0


def test_extract_tool_results_pulls_name_error_and_text():
    entries = [
        _tool_result("bash", True, "Command exited with code 1", "t1"),
        {"type": "session"},  # non-message entries ignored
        _tool_result("read", False, "file contents here", "t2"),
    ]
    results = extract_tool_results(entries)
    assert len(results) == 2
    assert results[0] == {
        "toolName": "bash", "isError": True,
        "text": "Command exited with code 1", "timestamp": "t1",
    }
    assert results[1]["isError"] is False


def test_detect_error_loop_fires_on_repeated_same_tool_failure():
    results = [
        {"toolName": "bash", "isError": True, "text": "Command exited with code 1", "timestamp": "t1"},
        {"toolName": "bash", "isError": True, "text": "Command exited with code 1", "timestamp": "t2"},
        {"toolName": "bash", "isError": True, "text": "Command exited with code 1", "timestamp": "t3"},
    ]
    alert = detect_error_loop(results, threshold=3)
    assert alert is not None
    assert "bash" in alert
    assert "repeated failure" in alert


def test_detect_error_loop_does_not_fire_on_mixed_success():
    results = [
        {"toolName": "bash", "isError": True, "text": "err", "timestamp": "t1"},
        {"toolName": "bash", "isError": False, "text": "ok", "timestamp": "t2"},
        {"toolName": "bash", "isError": True, "text": "err", "timestamp": "t3"},
    ]
    assert detect_error_loop(results, threshold=3) is None


def test_detect_error_loop_does_not_fire_below_threshold():
    results = [
        {"toolName": "bash", "isError": True, "text": "err", "timestamp": "t1"},
        {"toolName": "bash", "isError": True, "text": "err", "timestamp": "t2"},
    ]
    assert detect_error_loop(results, threshold=3) is None


def test_touched_files_from_git_lists_porcelain_paths(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "foo.py").write_text("x = 1\n")
    files = touched_files_from_git(str(tmp_path))
    assert "foo.py" in files


def test_touched_files_from_git_returns_empty_when_not_a_repo(tmp_path):
    files = touched_files_from_git(str(tmp_path))
    assert files == []


@pytest.mark.asyncio
async def test_watch_run_writes_progress_and_stops_when_not_alive(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_dir = tmp_path / "proj"
    working_dir.mkdir()

    adapter = MagicMock()
    adapter.transcript_path.return_value = None  # no transcript available -> git-only

    alive_flags = [True, False]

    def is_alive():
        return alive_flags.pop(0) if alive_flags else False

    await watch_run(
        run_dir=run_dir,
        adapter=adapter,
        working_dir=str(working_dir),
        started_at=datetime.datetime.now(datetime.timezone.utc),
        is_alive=is_alive,
        watch_interval_s=0.01,
        idle_threshold_s=300.0,
        error_loop_threshold=3,
    )

    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["status"] == "running"
    assert progress["alert"] is None
    assert progress["files_touched"] == []


@pytest.mark.asyncio
async def test_watch_run_raises_stall_alert_when_idle_too_long(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_dir = tmp_path / "proj"
    working_dir.mkdir()

    adapter = MagicMock()
    adapter.transcript_path.return_value = None

    alive_flags = [True, False]

    def is_alive():
        return alive_flags.pop(0) if alive_flags else False

    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1000)

    await watch_run(
        run_dir=run_dir,
        adapter=adapter,
        working_dir=str(working_dir),
        started_at=long_ago,
        is_alive=is_alive,
        watch_interval_s=0.01,
        idle_threshold_s=300.0,
        error_loop_threshold=3,
    )

    progress = json.loads((run_dir / "progress.json").read_text())
    assert progress["alert"] is not None
    assert "idle" in progress["alert"] or "no transcript activity" in progress["alert"]
