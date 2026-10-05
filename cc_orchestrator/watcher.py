from __future__ import annotations

import asyncio
import datetime
import json
import subprocess
from pathlib import Path
from typing import Callable

from cc_orchestrator.workers.base import WorkerAdapter


def parse_new_entries(path: Path, offset: int) -> tuple[list[dict], int]:
    """Read complete new JSON lines from path starting at byte offset.

    Returns (entries, new_offset). An incomplete trailing line (no closing
    newline yet -- the writer is mid-append) is held back: new_offset stops
    just before it, so the next call re-reads and completes it once it's
    whole.
    """
    if not path.exists():
        return [], offset

    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()

    if not data:
        return [], offset

    text = data.decode("utf-8", errors="replace")
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")
    if not ends_with_newline and lines and lines[-1] != "":
        incomplete = lines.pop()
        consumed = len(data) - len(incomplete.encode("utf-8"))
    else:
        consumed = len(data)

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return entries, offset + consumed


def extract_tool_results(entries: list[dict]) -> list[dict]:
    """Pull {toolName, isError, text, timestamp} out of toolResult message entries."""
    out = []
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") != "toolResult":
            continue
        content = message.get("content") or []
        text = ""
        if content and isinstance(content, list) and isinstance(content[0], dict):
            text = content[0].get("text", "")
        out.append({
            "toolName": message.get("toolName"),
            "isError": bool(message.get("isError")),
            "text": text,
            "timestamp": entry.get("timestamp"),
        })
    return out


def detect_error_loop(tool_results: list[dict], threshold: int = 3) -> str | None:
    """Flag when the last `threshold` toolResults are all errors from the same
    tool with matching error text -- a cheap pattern match, not a judgment
    call, meant only to say "look closer here."
    """
    if len(tool_results) < threshold:
        return None
    window = tool_results[-threshold:]
    if not all(r["isError"] for r in window):
        return None
    names = {r["toolName"] for r in window}
    if len(names) != 1:
        return None
    texts = [r["text"] for r in window]
    if not texts[0]:
        return None
    prefix = texts[0][:50]
    if not all(prefix in t for t in texts):
        return None
    tool_name = window[-1]["toolName"]
    latest_text = texts[-1][:200]
    return f"repeated failure: {tool_name} failing repeatedly, latest: {latest_text}"


def touched_files_from_git(working_dir: str) -> list[str]:
    """Cheap, always-safe signal: files git sees as touched in working_dir.
    Returns [] if working_dir isn't a git repo or git isn't on PATH --
    never raises.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=working_dir, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        files.append(line.split(maxsplit=1)[-1])
    return files


async def watch_run(
    run_dir: Path,
    adapter: WorkerAdapter,
    working_dir: str,
    started_at: datetime.datetime,
    is_alive: Callable[[], bool],
    watch_interval_s: float = 60.0,
    idle_threshold_s: float = 300.0,
    error_loop_threshold: int = 3,
) -> None:
    """Background loop: poll the adapter's transcript (if any) plus git state,
    write a live snapshot to run_dir/progress.json, until is_alive() returns
    False. Never raises -- a watcher failure must not take down the worker
    it's observing.
    """
    progress_file = run_dir / "progress.json"
    transcript_file: Path | None = None
    offset = 0
    tool_results: list[dict] = []
    last_entry_time = started_at

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            if transcript_file is None:
                transcript_file = adapter.transcript_path(working_dir, started_at)

            if transcript_file is not None:
                entries, offset = parse_new_entries(transcript_file, offset)
                if entries:
                    last_entry_time = now
                    tool_results.extend(extract_tool_results(entries))
                    tool_results = tool_results[-20:]

            idle_seconds = (now - last_entry_time).total_seconds()

            alert = detect_error_loop(tool_results, error_loop_threshold)
            if alert is None and idle_seconds > idle_threshold_s and is_alive():
                alert = f"no transcript activity for {int(idle_seconds)}s — process alive but idle"

            files_touched = touched_files_from_git(working_dir)

            progress_file.write_text(json.dumps({
                "status": "running",
                "alert": alert,
                "idle_seconds": int(idle_seconds),
                "files_touched": files_touched,
            }))
        except Exception:
            # A single tick's failure (e.g. a transient stat() race, or an
            # adapter misbehaving) must not kill the loop -- the docstring's
            # "never raises" promise holds even when a tick itself blows up.
            pass

        if not is_alive():
            break
        await asyncio.sleep(watch_interval_s)
