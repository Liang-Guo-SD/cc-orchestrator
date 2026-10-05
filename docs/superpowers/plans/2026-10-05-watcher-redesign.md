# Watcher Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop qoder as a worker, make `dispatch_worker` non-blocking, and add a background
watcher that tails Pi's own session transcript and git state so Claude Code gets an early
trip-wire signal (stall/error-loop) instead of discovering a stuck run only after the full
timeout elapses.

**Architecture:** `dispatch_worker` spawns the Pi subprocess and a background `asyncio.Task`
(`_run_and_finalize`) that races a second `asyncio.Task` (the watcher) against the subprocess's
own completion, then returns `{run_id, status: "running"}` immediately. The watcher polls Pi's
transcript (path reconstructed from Pi's own directory-naming scheme) plus `git status`/`git
diff --stat`, writing a live snapshot to `run_dir/progress.json`. When the subprocess exits, the
existing summarization/verification pipeline runs once and writes `run_dir/result.json`. Two new
MCP tools (`get_run_status`, `get_run_progress`) read whichever of those two files exists and
return one unified response shape regardless of whether the run is still going.

**Tech Stack:** Python 3.11+, asyncio, MCP server SDK, anthropic SDK (Haiku), pyyaml, pytest +
pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-05-watcher-redesign-design.md`

## Global Constraints

- Python `>=3.11` (per `pyproject.toml`); no new runtime dependencies beyond what's already
  declared (`mcp[cli]>=2.0.0`, `anthropic>=0.40.0`, `pyyaml>=6.0.0`).
- All subprocess work stays on `asyncio` (never `subprocess.run` for the worker CLI itself —
  `git`/status-check calls are fine as blocking `subprocess.run` with a short timeout since
  they're expected to return near-instantly).
- Response shape for `get_run_status`/`get_run_progress`/`result.json` is exactly: `run_id,
  agent, status, summary, alert, idle_seconds, files_touched, claims, verification_targets,
  raw_output_path, parent_run_id` — the same keys whether the run is mid-flight or finished.
- Defaults: `watch_interval_s=60`, `idle_threshold_s=300`, `error_loop_threshold=3` (all
  overridable via `~/.cc-orchestrator/config.yaml`).
- `session` values outside `{"new", "continue"}` are rejected with a clear error (no more
  silent no-op fallback for garbage input).
- No OS-level PID polling: "process alive" is derived from whether the in-process worker task
  has completed, not from checking a PID — this is simpler and exactly as correct since the
  worker task's lifetime *is* the subprocess's lifetime.

## Review Focus

- **Malformed/partial JSONL line mid-transcript** (Pi is killed or crashes mid-write, leaving a
  half-written final line) — `parse_new_entries` must not crash and must not advance past the
  incomplete line (so the rest of it gets picked up once it's actually complete on a later
  tick). Covered in Task 3.
- **No transcript directory at all** (Pi never created a session file, or a future adapter has
  no `transcript_path` implementation) — the watcher must still run on git-state alone rather
  than raising. Covered in Task 3.
- **Garbage `session` value** (e.g. a stray UUID, as actually happened in the incident the
  findings doc records) — must be rejected with a clear error instead of silently behaving like
  every other value. Covered in Task 5.
- **Unknown `run_id` passed to `get_run_status`/`get_run_progress`/`get_run_output`** (typo, or
  a run from a wiped `~/.cc-orchestrator`) — must return a clear error dict/string, never raise.
  Covered in Task 5.
- **Process that exits before the watcher's first tick runs** (fast failure — e.g. missing
  binary, exit code 127) — `_run_and_finalize` must still cancel the watcher cleanly and produce
  a correct terminal `result.json`, not hang waiting on a watcher that's chasing a process that
  no longer exists. Covered in Task 5.

---

## Task 1: Drop qoder worker and role-convention machinery

**Files:**
- Delete: `cc_orchestrator/workers/qoder.py`
- Modify: `config.example.yaml`
- Modify: `cc_orchestrator/server.py` (remove `ROLE_ASSIGNMENTS`, `_role_mismatch`,
  `override_role` param; `_get_adapter` drops the `qoder` entry)
- Modify: `tests/test_server.py` (remove qoder/role-mismatch tests, update remaining tests to
  drop `override_role=`)
- Modify: `tests/test_config.py` (drop `qoder` key from fixture YAML)

**Interfaces:**
- Produces: `_get_adapter(agent: str, config) -> WorkerAdapter | None` now only recognizes
  `"pi"`. `_handle_dispatch(agent, prompt, session, working_dir, timeout_s, task_type) -> dict`
  — `override_role` parameter removed (this signature changes again in Task 5; for this task it
  keeps its current blocking behavior, just without the role check).

- [ ] **Step 1: Update tests/test_server.py to the post-qoder expectations**

Replace the full file content with:

```python
import json
import time
from pathlib import Path
import pytest

from cc_orchestrator.server import _handle_dispatch, _handle_get_output


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
""")
    return cfg


def _setup_home(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")


@pytest.mark.asyncio
async def test_dispatch_returns_run_result_dict(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    result = await _handle_dispatch(
        agent="pi",
        prompt="echo hello",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )

    assert "run_id" in result
    assert result["agent"] == "pi"
    assert result["status"] in ("completed", "failed", "low_confidence")
    assert "claims" in result
    assert "verification_targets" in result
    assert "raw_output_path" in result


@pytest.mark.asyncio
async def test_dispatch_unknown_agent_returns_error(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    result = await _handle_dispatch(
        agent="unknown_agent_xyz",
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )
    assert result["status"] == "failed"
    assert "unknown" in result["summary"].lower()


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
""")

    import asyncio

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

    assert elapsed < 1.8  # two serial subprocesses would take >= 2s
    assert all("run_id" in r for r in results)


@pytest.mark.asyncio
async def test_get_output_returns_last_n_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runs_dir = tmp_path / ".cc-orchestrator" / "runs" / "run-test-001"
    runs_dir.mkdir(parents=True)
    output_file = runs_dir / "output.txt"
    output_file.write_text("\n".join(f"line {i}" for i in range(500)))

    result = _handle_get_output(run_id="run-test-001", lines=10)
    returned_lines = result.strip().splitlines()
    assert len(returned_lines) == 10
    assert returned_lines[-1] == "line 499"
```

- [ ] **Step 2: Run tests to verify the removed-role-logic tests fail against current code**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_server.py -v`
Expected: `test_dispatch_unknown_agent_returns_error` fails (current code requires
`override_role=True` to reach the unknown-agent path since `task_type="implementation"` defaults
to requiring `agent="pi"`, which is already satisfied here — actually check: this one should
currently PASS since agent is unknown regardless of role. The two qoder/role tests are simply
gone from the file, so the only true failure expected here is none yet — this step exists to
confirm the file parses and the surviving tests still pass before the qoder deletion, so you
have a clean baseline). If anything errors besides expected qoder-removal, stop and investigate.

- [ ] **Step 3: Delete the qoder adapter**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && rm cc_orchestrator/workers/qoder.py`

- [ ] **Step 4: Remove the qoder block from config.example.yaml**

Replace the full file content with:

```yaml
max_retries: 3

# Dispatch independent sub-tasks in parallel — dispatch_worker runs each CLI
# as an asyncio subprocess, so concurrent calls don't queue behind each
# other. Fire off several dispatch_worker calls in the same turn for
# unrelated sub-tasks (e.g. two independent stories, or an implementation
# task alongside a review task) and poll each run_id independently.
#
# OpenCode and Qoder were removed as workers (see README) — pi is the only
# supported worker today. The adapter interface in cc_orchestrator/workers/
# is written so a new worker is just a new adapter class, not an interface
# change.

workers:
  pi:
    # Invocation: pi --approve --print <prompt>
    # -p/--print is required for non-interactive mode; without it pi also
    # drops into its interactive TUI and never reports completion.
    bin: pi
    extra_flags:
      - "--approve"            # trust project-local files for this run
      - "--print"              # non-interactive mode: process prompt and exit
    new_session_flag: ""
    continue_flag: "--continue"
    session_id_flag: "--session"

haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"

# Watcher tuning (optional; defaults shown). The watcher is a background
# task that tails pi's own session transcript and git status while a
# dispatched run is still going, so a stuck or error-looping run can be
# flagged well before timeout_s elapses instead of only being discoverable
# after the fact.
watch_interval_s: 60       # seconds between watcher polls
idle_threshold_s: 300      # no transcript activity for this long -> stall alert
error_loop_threshold: 3    # this many consecutive same-tool isError results -> error-loop alert
```

- [ ] **Step 5: Drop qoder from config.example.yaml's loader fixture in test_config.py**

In `tests/test_config.py`, remove the `qoder:` block (4 lines) from both YAML fixtures in
`test_load_config_parses_valid_yaml` and `test_load_config_defaults_to_home_dir`, leaving only
the `pi:` worker entry in each.

- [ ] **Step 6: Remove role-convention machinery from server.py**

In `cc_orchestrator/server.py`:
- Delete the `ROLE_ASSIGNMENTS` dict and the `_role_mismatch` function entirely.
- In `_get_adapter`, change the `adapters` dict to just `{"pi": PiAdapter}` and delete the
  `from cc_orchestrator.workers.qoder import QoderAdapter` import.
- In `_handle_dispatch`, delete the `override_role: bool = False` parameter and the `if not
  override_role: ... return _error_result(...)` block that calls `_role_mismatch`.
- In the `dispatch_worker` MCP tool function, delete the `override_role: bool = False`
  parameter, the corresponding argument in the `_handle_dispatch(...)` call, and rewrite the
  docstring to remove the role-convention description (Task 5 rewrites this docstring fully
  anyway; for now just remove the stale role-convention paragraph so nothing references deleted
  code).

- [ ] **Step 7: Run tests to verify everything passes**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/ -v`
Expected: all tests PASS (no qoder references remain anywhere in the test suite or source).

- [ ] **Step 8: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add -A
git commit -m "$(cat <<'EOF'
refactor: drop qoder worker and role-convention machinery

Qoder is removed as a supported worker; pi is the only worker now.
With one worker there's nothing left for ROLE_ASSIGNMENTS/
_role_mismatch/override_role to enforce, so that machinery is deleted
rather than left as dead code with one legal choice.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `on_started` hook and `transcript_path()` to the adapter interface

**Files:**
- Modify: `cc_orchestrator/workers/base.py`
- Modify: `cc_orchestrator/workers/cli_adapter.py`
- Modify: `cc_orchestrator/workers/pi.py`
- Test: `tests/test_workers.py`

**Interfaces:**
- Produces: `WorkerAdapter.run(..., on_started: Callable[[asyncio.subprocess.Process], None] |
  None = None) -> AdapterResult` (new optional param, default `None`, backward compatible).
- Produces: `WorkerAdapter.transcript_path(working_dir: str, started_after: datetime.datetime) ->
  Path | None` (new concrete method on the base class, default returns `None`; `PiAdapter`
  overrides it).
- Consumes (Task 5/3): `PiAdapter().transcript_path(...)` to locate the live session file;
  `adapter.run(..., on_started=...)` to capture the subprocess for `meta.json`'s `pid` field.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workers.py` (append; keep existing imports and helpers):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_workers.py -v`
Expected: FAIL — `run()` doesn't accept `on_started`, and `transcript_path` doesn't exist yet.

- [ ] **Step 3: Implement `on_started` and the base `transcript_path`**

In `cc_orchestrator/workers/base.py`, replace the full file content with:

```python
from __future__ import annotations

import asyncio
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class AdapterResult:
    exit_code: int
    session_id: str | None = None


class WorkerAdapter(ABC):
    """Each worker CLI adapter implements this interface."""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
        on_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> AdapterResult:
        """Spawn the worker CLI, write all output to output_file, return exit code.

        Must use asyncio subprocess (not subprocess.run) so concurrent
        dispatch_worker calls can run multiple workers in parallel on one
        event loop. If on_started is given, it is called once, synchronously,
        right after the subprocess is spawned (before waiting on it), so a
        caller can record the process for inspection (e.g. its pid) without
        blocking on completion.
        """
        ...

    @abstractmethod
    def extraction_patterns(self) -> dict[str, str]:
        """Return regex patterns this adapter knows how to extract.
        Keys match Claims field names. Values are regex strings with one capture group."""
        ...

    def transcript_path(
        self, working_dir: str, started_after: datetime.datetime
    ) -> Path | None:
        """Return the path to this adapter's own CLI's session transcript for
        working_dir, if the underlying CLI keeps one on disk and this adapter
        knows where. Default: no such transcript is known, so callers (the
        watcher) fall back to git-state observation alone.
        """
        return None
```

In `cc_orchestrator/workers/cli_adapter.py`, apply this diff:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.session_store import load_session, save_session
from cc_orchestrator.workers.base import AdapterResult, WorkerAdapter


class CliAdapter(WorkerAdapter):
    """Generic subprocess-based adapter for a headless-CLI worker agent."""

    _agent_name: str

    def __init__(
        self,
        config: WorkerConfig,
        runs_dir: Path,
        sessions_dir: Path,
    ) -> None:
        self._cfg = config
        self._runs_dir = runs_dir
        self._sessions_dir = sessions_dir

    def extraction_patterns(self) -> dict[str, str]:
        return {}  # standard tool outputs; built-ins cover it

    async def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
        on_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> AdapterResult:
        project_key = Path(working_dir).name
        cmd = [self._cfg.bin]
        cmd += list(self._cfg.command_prefix)

        if self._cfg.extra_flags:
            cmd += self._cfg.extra_flags

        if session == "continue":
            stored = load_session(self._sessions_dir, self._agent_name, project_key)
            if stored and self._cfg.continue_flag:
                cmd += [self._cfg.continue_flag]
                if self._cfg.session_id_flag:
                    cmd += [self._cfg.session_id_flag, stored]
            else:
                session = "new"  # fall back silently

        if session == "new" and self._cfg.new_session_flag:
            cmd += [self._cfg.new_session_flag]

        cmd += [prompt]

        output_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=working_dir,
            )
        except FileNotFoundError:
            output_file.write_text(f"error: binary '{self._cfg.bin}' not found on PATH\n")
            return AdapterResult(exit_code=127, session_id=None)

        if on_started is not None:
            on_started(proc)

        async def _drain_and_wait() -> int:
            with output_file.open("wb") as out_fh:
                async for chunk in proc.stdout:
                    out_fh.write(chunk)
            return await proc.wait()

        try:
            returncode = await asyncio.wait_for(_drain_and_wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AdapterResult(exit_code=124, session_id=None)

        return AdapterResult(exit_code=returncode, session_id=None)
```

In `cc_orchestrator/workers/pi.py`, replace the full file content with:

```python
from __future__ import annotations

import datetime
from pathlib import Path

from cc_orchestrator.workers.cli_adapter import CliAdapter


class PiAdapter(CliAdapter):
    """Pi Agent CLI adapter."""
    _agent_name = "pi"

    def extraction_patterns(self) -> dict[str, str]:
        return {}

    def transcript_path(
        self, working_dir: str, started_after: datetime.datetime
    ) -> Path | None:
        """Locate pi's own session transcript for working_dir.

        pi keeps a per-project session log at
        ~/.pi/agent/sessions/<mangled-abs-path>/<timestamp>_<uuid>.jsonl, where
        mangled-abs-path is the absolute working directory with every "/"
        replaced by "-", with one extra leading "-" and a trailing "--"
        (confirmed by inspecting real session directories: the absolute path
        "/Users/liangguo/Feishubot_V4" maps to the directory name
        "--Users-liangguo-Feishubot_V4--"). This is deliberately independent
        of cc_orchestrator.session_store's project_key (which is just the
        working directory's basename, used for our own unrelated
        session-continue feature) -- conflating the two would point the
        watcher at the wrong directory.

        Among files in that directory modified at or after `started_after`
        (minus a small clock-skew buffer), returns the earliest one -- the
        file closest to our own dispatch's start time. If two dispatches
        start within the buffer window on the same working_dir, this can
        pick the wrong one; this is a known heuristic limitation, not a
        correctness guarantee.
        """
        abs_path = str(Path(working_dir).resolve())
        mangled = "-" + abs_path.replace("/", "-") + "--"
        session_dir = Path.home() / ".pi" / "agent" / "sessions" / mangled
        if not session_dir.exists():
            return None

        cutoff = started_after.timestamp() - 5  # buffer for clock skew
        candidates = sorted(
            (p for p in session_dir.glob("*.jsonl") if p.stat().st_mtime >= cutoff),
            key=lambda p: p.stat().st_mtime,
        )
        return candidates[0] if candidates else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_workers.py -v`
Expected: PASS (all tests, including the pre-existing ones — confirm no regressions).

- [ ] **Step 5: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add cc_orchestrator/workers/base.py cc_orchestrator/workers/cli_adapter.py cc_orchestrator/workers/pi.py tests/test_workers.py
git commit -m "$(cat <<'EOF'
feat: add on_started hook and transcript_path to the adapter interface

on_started lets a caller observe the spawned subprocess (e.g. its pid)
without blocking on completion. transcript_path lets an adapter expose
where its underlying CLI keeps its own session transcript, if any --
PiAdapter implements it by replicating pi's directory-naming scheme,
confirmed against real session files under ~/.pi/agent/sessions/.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Build the watcher module

**Files:**
- Create: `cc_orchestrator/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Consumes: `WorkerAdapter.transcript_path(working_dir, started_after) -> Path | None` (Task 2).
- Produces: `watch_run(run_dir: Path, adapter: WorkerAdapter, working_dir: str, started_at:
  datetime.datetime, is_alive: Callable[[], bool], watch_interval_s: float = 60.0,
  idle_threshold_s: float = 300.0, error_loop_threshold: int = 3) -> None` (Task 5 runs this as a
  background task alongside the worker task).
- Produces (used directly by its own tests, not by Task 5): `parse_new_entries(path: Path,
  offset: int) -> tuple[list[dict], int]`, `extract_tool_results(entries: list[dict]) ->
  list[dict]`, `detect_error_loop(tool_results: list[dict], threshold: int) -> str | None`,
  `touched_files_from_git(working_dir: str) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watcher.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_watcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc_orchestrator.watcher'`.

- [ ] **Step 3: Implement the watcher module**

Create `cc_orchestrator/watcher.py`:

```python
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

        if not is_alive():
            break
        await asyncio.sleep(watch_interval_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_watcher.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add cc_orchestrator/watcher.py tests/test_watcher.py
git commit -m "$(cat <<'EOF'
feat: add watcher module for transcript/git-based run observation

Polls an adapter's session transcript (when available) plus git
status while a dispatched run is still going, detecting repeated
same-tool isError loops and idle stalls as cheap pattern-matched
trip-wires -- not LLM judgments -- so a stuck run can be flagged well
before timeout_s elapses. Falls back to git-only observation when no
transcript is discoverable.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `summarize_progress` for the running-state one-liner

**Files:**
- Modify: `cc_orchestrator/summarizer.py`
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Produces: `summarize_progress(progress: dict, haiku_model: str, api_key: str | None) -> str`
  where `progress` has keys `idle_seconds: int`, `files_touched: list[str]`, `alert: str |
  None`. Used by Task 5's `get_run_progress` handler for runs still in progress.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_summarizer.py`:

```python
from cc_orchestrator.summarizer import summarize_progress


def test_summarize_progress_uses_haiku_when_api_key_present():
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value = _mock_anthropic("Editing app/foo.py; 2 files touched so far.")
        summary = summarize_progress(
            {"idle_seconds": 12, "files_touched": ["app/foo.py", "app/bar.py"], "alert": None},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="test-key",
        )
    assert "foo.py" in summary or "touched" in summary


def test_summarize_progress_falls_back_without_api_key():
    summary = summarize_progress(
        {"idle_seconds": 12, "files_touched": ["app/foo.py"], "alert": None},
        haiku_model="claude-haiku-4-5-20251001",
        api_key=None,
    )
    assert "1" in summary or "app/foo.py" in summary


def test_summarize_progress_falls_back_on_api_error():
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = (
            __import__("anthropic").AuthenticationError.__new__(__import__("anthropic").AuthenticationError)
        )
        summary = summarize_progress(
            {"idle_seconds": 5, "files_touched": [], "alert": None},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="bad-key",
        )
    assert isinstance(summary, str) and summary != ""


def test_summarize_progress_surfaces_alert_in_fallback():
    summary = summarize_progress(
        {"idle_seconds": 400, "files_touched": [], "alert": "no transcript activity for 400s — process alive but idle"},
        haiku_model="claude-haiku-4-5-20251001",
        api_key=None,
    )
    assert "idle" in summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_summarizer.py -v`
Expected: FAIL with `ImportError: cannot import name 'summarize_progress'`.

- [ ] **Step 3: Implement `summarize_progress`**

Append to `cc_orchestrator/summarizer.py` (after the existing `summarize` function):

```python
_PROGRESS_HAIKU_PROMPT = """\
A background coding agent dispatched as a subprocess is still running.
Here are the latest observed facts about it:
- idle_seconds: {idle_seconds} (seconds since its session transcript last grew)
- files_touched: {files_touched}
- alert: {alert}

Write ONE short sentence (at most 25 words) describing its current status
for a human monitoring it. Respond with only the sentence, no quotes, no
markdown."""


def _fallback_progress_summary(progress: dict) -> str:
    if progress.get("alert"):
        return progress["alert"]
    files = progress.get("files_touched") or []
    idle = progress.get("idle_seconds", 0)
    return f"running; {len(files)} file(s) touched so far; idle {idle}s"


def summarize_progress(
    progress: dict,
    haiku_model: str,
    api_key: str | None,
) -> str:
    """Turn the watcher's raw facts (idle_seconds, files_touched, alert) into
    a one-line human-readable status for a run that is still going. Falls
    back to a deterministic sentence when no API key is configured or the
    API call fails -- this must never raise.
    """
    if api_key is None:
        return _fallback_progress_summary(progress)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=haiku_model,
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": _PROGRESS_HAIKU_PROMPT.format(
                    idle_seconds=progress.get("idle_seconds", 0),
                    files_touched=progress.get("files_touched") or [],
                    alert=progress.get("alert"),
                ),
            }],
        )
        return msg.content[0].text.strip()
    except (anthropic.AuthenticationError, anthropic.APIError):
        return _fallback_progress_summary(progress)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_summarizer.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add cc_orchestrator/summarizer.py tests/test_summarizer.py
git commit -m "$(cat <<'EOF'
feat: add summarize_progress for mid-run status one-liners

Turns the watcher's raw facts (idle_seconds, files_touched, alert)
into a human-readable sentence via Haiku, with a deterministic
fallback when no API key is configured or the call fails.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Rewire server.py for non-blocking dispatch

**Files:**
- Modify: `cc_orchestrator/server.py`
- Modify: `cc_orchestrator/config.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `watch_run(...)` (Task 3), `summarize_progress(...)` (Task 4),
  `adapter.run(..., on_started=...)` (Task 2), `adapter.transcript_path(...)` (Task 2, used
  indirectly via `watch_run`).
- Produces: `_handle_dispatch(agent, prompt, session, working_dir, timeout_s, task_type) -> dict`
  now returns `{"run_id": ..., "status": "running"}` immediately (no `override_role`).
  `_handle_get_status(run_id) -> dict`, `_handle_get_progress(run_id) -> dict`. MCP tools
  `dispatch_worker`, `get_run_status`, `get_run_progress`, `get_run_output` (unchanged shape for
  the last one).

- [ ] **Step 1: Add watcher tuning fields to OrchestratorConfig**

In `cc_orchestrator/config.py`, modify the `OrchestratorConfig` dataclass and `load_config`:

```python
@dataclass
class OrchestratorConfig:
    max_retries: int
    workers: dict[str, WorkerConfig]
    haiku_model: str
    anthropic_api_key_env: str
    watch_interval_s: float = 60.0
    idle_threshold_s: float = 300.0
    error_loop_threshold: int = 3
```

And in `load_config`, change the final `return`:

```python
    return OrchestratorConfig(
        max_retries=int(data.get("max_retries", 3)),
        workers=workers,
        haiku_model=data.get("haiku_model", "claude-haiku-4-5-20251001"),
        anthropic_api_key_env=data.get("anthropic_api_key_env", "ANTHROPIC_API_KEY"),
        watch_interval_s=float(data.get("watch_interval_s", 60.0)),
        idle_threshold_s=float(data.get("idle_threshold_s", 300.0)),
        error_loop_threshold=int(data.get("error_loop_threshold", 3)),
    )
```

- [ ] **Step 2: Write the failing tests for the new server behavior**

Replace `tests/test_server.py`'s content (building on Task 1's version) with:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_server.py -v`
Expected: FAIL — `_handle_get_status`/`_handle_get_progress` don't exist yet; `_handle_dispatch`
still blocks and returns the old synchronous shape.

- [ ] **Step 4: Rewire server.py**

Replace the full content of `cc_orchestrator/server.py` with:

```python
from __future__ import annotations

import asyncio
import datetime
import json
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP as _MCPClass
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCPClass  # type: ignore[assignment]

from cc_orchestrator.config import load_config
from cc_orchestrator.summarizer import summarize, summarize_progress
from cc_orchestrator.verifier import generate_targets
from cc_orchestrator.watcher import watch_run
from cc_orchestrator.workers.pi import PiAdapter

mcp = _MCPClass("cc-orchestrator")

# Background tasks must be referenced somewhere, or asyncio may garbage-collect
# a running task mid-execution. This set is that reference.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> asyncio.Task:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


def _base_dir() -> Path:
    return Path.home() / ".cc-orchestrator"


def _make_run_id(agent: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{ts}-{agent}"


def _get_adapter(agent: str, config):
    adapters = {"pi": PiAdapter}
    if agent not in adapters:
        return None
    worker_cfg = config.workers.get(agent)
    if worker_cfg is None:
        return None
    runs_dir = _base_dir() / "runs"
    sessions_dir = _base_dir() / "sessions"
    return adapters[agent](worker_cfg, runs_dir, sessions_dir)


def _error_result(agent: str, summary: str) -> dict:
    return {
        "run_id": "run-error",
        "agent": agent,
        "status": "failed",
        "summary": summary,
        "alert": None,
        "idle_seconds": 0,
        "files_touched": [],
        "claims": {"exit_code": 1, "tests_passed": None, "tests_failed": None,
                   "tests_error": None, "files_changed": None, "commit_hash": None,
                   "important_findings": []},
        "verification_targets": [],
        "raw_output_path": "",
        "parent_run_id": None,
    }


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


async def _run_and_finalize(
    run_id: str,
    run_dir: Path,
    adapter,
    prompt: str,
    session: str,
    working_dir: str,
    timeout_s: int,
    task_type: str,
    config,
    started_at: datetime.datetime,
) -> None:
    output_file = run_dir / "output.txt"
    meta_file = run_dir / "meta.json"

    def _on_started(proc) -> None:
        meta = _read_json(meta_file) or {}
        meta["pid"] = proc.pid
        meta_file.write_text(json.dumps(meta))

    worker_task = asyncio.ensure_future(
        adapter.run(
            prompt=prompt,
            session=session,
            working_dir=working_dir,
            timeout_s=timeout_s,
            output_file=output_file,
            on_started=_on_started,
        )
    )

    watcher_task = asyncio.ensure_future(
        watch_run(
            run_dir=run_dir,
            adapter=adapter,
            working_dir=working_dir,
            started_at=started_at,
            is_alive=lambda: not worker_task.done(),
            watch_interval_s=config.watch_interval_s,
            idle_threshold_s=config.idle_threshold_s,
            error_loop_threshold=config.error_loop_threshold,
        )
    )

    adapter_result = await worker_task
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass

    raw_text = output_file.read_text(errors="replace") if output_file.exists() else ""
    api_key = os.environ.get(config.anthropic_api_key_env)

    claims, summary, confidence = summarize(
        raw_text=raw_text,
        exit_code=adapter_result.exit_code,
        task_type=task_type,
        adapter_patterns=adapter.extraction_patterns(),
        haiku_model=config.haiku_model,
        api_key=api_key,
    )

    if adapter_result.exit_code == 124:
        status = "timeout"
    elif adapter_result.exit_code != 0:
        status = "failed"
    elif confidence < 0.6:
        status = "low_confidence"
    else:
        status = "completed"

    targets = [] if status == "low_confidence" else generate_targets(claims, working_dir)

    result = {
        "run_id": run_id,
        "agent": adapter._agent_name,
        "status": status,
        "summary": summary,
        "alert": None,
        "idle_seconds": 0,
        "files_touched": [],
        "claims": {
            "tests_passed": claims.tests_passed,
            "tests_failed": claims.tests_failed,
            "tests_error": claims.tests_error,
            "files_changed": claims.files_changed,
            "commit_hash": claims.commit_hash,
            "exit_code": claims.exit_code,
            "important_findings": claims.important_findings,
        },
        "verification_targets": [
            {"command": t.command, "expected_pattern": t.expected_pattern, "description": t.description}
            for t in targets
        ],
        "raw_output_path": str(output_file),
        "parent_run_id": None,
    }
    (run_dir / "result.json").write_text(json.dumps(result))


async def _handle_dispatch(
    agent: str,
    prompt: str,
    session: str,
    working_dir: str,
    timeout_s: int,
    task_type: str,
) -> dict:
    if session not in ("new", "continue"):
        return _error_result(
            agent,
            f"invalid session value '{session}'; must be 'new' or 'continue' "
            f"('continue' is currently a no-op that falls back to 'new' -- "
            f"see README \"Known Limitations\")",
        )

    config = load_config()
    adapter = _get_adapter(agent, config)
    if adapter is None:
        return _error_result(agent, f"unknown agent '{agent}'; valid agents: pi")

    run_id = _make_run_id(agent)
    run_dir = _base_dir() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.datetime.now(datetime.timezone.utc)

    (run_dir / "meta.json").write_text(json.dumps({
        "agent": agent,
        "task_type": task_type,
        "prompt": prompt,
        "working_dir": working_dir,
        "started_at": started_at.isoformat(),
        "timeout_s": timeout_s,
    }))

    _track(asyncio.create_task(_run_and_finalize(
        run_id, run_dir, adapter, prompt, session, working_dir, timeout_s,
        task_type, config, started_at,
    )))

    return {"run_id": run_id, "status": "running"}


def _handle_get_status(run_id: str) -> dict:
    run_dir = _base_dir() / "runs" / run_id

    result = _read_json(run_dir / "result.json")
    if result is not None:
        return result

    meta = _read_json(run_dir / "meta.json")
    if meta is None:
        return _error_result("", f"no run found for run_id '{run_id}'")

    progress = _read_json(run_dir / "progress.json") or {}
    return {
        "run_id": run_id,
        "agent": meta.get("agent"),
        "status": "running",
        "summary": "",
        "alert": progress.get("alert"),
        "idle_seconds": progress.get("idle_seconds", 0),
        "files_touched": progress.get("files_touched", []),
        "claims": None,
        "verification_targets": [],
        "raw_output_path": str(run_dir / "output.txt"),
        "parent_run_id": None,
    }


def _handle_get_progress(run_id: str) -> dict:
    status = _handle_get_status(run_id)
    if status.get("status") != "running":
        return status

    config = load_config()
    api_key = os.environ.get(config.anthropic_api_key_env)
    status["summary"] = summarize_progress(
        {
            "idle_seconds": status["idle_seconds"],
            "files_touched": status["files_touched"],
            "alert": status["alert"],
        },
        config.haiku_model,
        api_key,
    )
    return status


def _handle_get_output(run_id: str, lines: int = 100) -> str:
    output_file = _base_dir() / "runs" / run_id / "output.txt"
    if not output_file.exists():
        return f"No output found for run_id '{run_id}'"
    all_lines = output_file.read_text(errors="replace").splitlines()
    return "\n".join(all_lines[-lines:])


@mcp.tool()
async def dispatch_worker(
    agent: str = "pi",
    prompt: str = "",
    session: str = "new",
    working_dir: str = ".",
    timeout_s: int = 1800,
    task_type: str = "implementation",
) -> dict:
    """Dispatch a task prompt to a worker CLI agent. Returns immediately with
    {run_id, status: "running"} -- this does NOT block until the worker
    finishes. Poll get_run_status (free) or get_run_progress (adds a small
    Haiku-generated summary) with the returned run_id to follow progress and
    get the final result.

    Issue several dispatch_worker calls in the same turn for independent
    sub-tasks and they run concurrently (each as its own asyncio subprocess);
    this is the intended way to parallelize unrelated work rather than
    dispatching one at a time.

    agent: the only supported worker today is "pi".

    session: "new" starts a fresh pi session. "continue" is currently a
    documented no-op that silently falls back to "new" -- pi session-id
    capture/resume is not implemented yet (see README "Known Limitations").
    Any other value is rejected with an error.
    """
    return await _handle_dispatch(agent, prompt, session, working_dir, timeout_s, task_type)


@mcp.tool()
def get_run_status(run_id: str) -> dict:
    """Return the latest known status for a dispatched run, at no API cost.

    While the run is still going (status == "running"), this reads a live
    snapshot written by a background watcher: idle_seconds (time since the
    worker's own session transcript last grew), files_touched (from git
    status), and alert (non-null when the watcher's repeated-error or
    idle-stall trip-wire has fired -- a cheap pattern match meant to prompt
    closer inspection well before timeout_s would otherwise be the first
    signal; it is not a verdict).

    Once the worker exits or timeout_s fires, status settles to
    "completed"/"failed"/"timeout"/"low_confidence" and the response includes
    the same claims/verification_targets dispatch_worker used to return
    synchronously in earlier versions of this tool.
    """
    return _handle_get_status(run_id)


@mcp.tool()
def get_run_progress(run_id: str) -> dict:
    """Like get_run_status, but while the run is still going, also spends a
    small Haiku API call to turn the raw watcher facts into a one-line
    human-readable status (e.g. "editing app/foo.py; 2 files touched so far").
    Poll this less often than get_run_status to control cost. Once the run
    has finished, behaves identically to get_run_status (no extra cost --
    the final result already has a summary).
    """
    return _handle_get_progress(run_id)


@mcp.tool()
def get_run_output(run_id: str, lines: int = 100) -> str:
    """Fetch the last N lines of raw stdout/stderr from a run.

    Note: for pi in headless mode (--print), this is often near-empty -- pi's
    real activity lives in its own session transcript, not stdout. Don't
    treat empty output here as evidence that nothing happened; use
    get_run_status/get_run_progress instead.
    """
    return _handle_get_output(run_id, lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/test_server.py -v`
Expected: PASS (all tests). If `test_run_status_is_running_before_completion_with_progress_fields`
is flaky, confirm `watch_interval_s: 0.01` is actually in the test's config fixture (the loop's
first write happens immediately, so flakiness here points at a config-loading mistake, not a
timing issue).

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && uv run pytest tests/ -v`
Expected: PASS (every test in the project).

- [ ] **Step 7: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add cc_orchestrator/server.py cc_orchestrator/config.py tests/test_server.py
git commit -m "$(cat <<'EOF'
feat: make dispatch_worker non-blocking with watcher-backed polling

dispatch_worker now spawns the worker + a background watcher task and
returns {run_id, status: "running"} immediately instead of blocking
for up to timeout_s. New get_run_status (free) and get_run_progress
(adds a Haiku one-liner) tools poll progress.json while running and
converge to the same response shape result.json provides once the
run finishes. session values outside {"new","continue"} are now
rejected instead of silently no-op'ing.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Update README and docs for the new architecture

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Rewrite README.md**

Replace the full content of `README.md` with:

```markdown
# cc-orchestrator

A lightweight MCP server for Claude Code that dispatches tasks to the Pi Agent CLI, watches its
real progress while it runs, and returns structured summaries with verification targets —
eliminating manual clipboard middleware and silent-timeout guessing games.

OpenCode and Qoder were supported in earlier versions and were removed: OpenCode's headless-mode
CLI invocation repeatedly regressed back to printing top-level `--help` instead of running the
prompt, across multiple fix attempts; Qoder was dropped to simplify the codebase to the worker
actually in active use. Pi has run reliably in headless mode. The adapter interface in
`cc_orchestrator/workers/` is written so adding a worker back is a new adapter class, not an
interface change.

## Installation

```bash
cd /path/to/cc-orchestrator
uv sync
```

**Note:** This project requires MCP v2+ (the MCP package has been updated;
`MCPServer` replaces `FastMCP`, but this is handled transparently by the server code).

## Configuration

```bash
cp config.example.yaml ~/.cc-orchestrator/config.yaml
```

Edit `~/.cc-orchestrator/config.yaml` and fill in the `# CONFIGURE THIS`
fields for the `pi` binary and session flags. `watch_interval_s`,
`idle_threshold_s`, and `error_loop_threshold` tune the background watcher
(see "How dispatch_worker works" below) and have sane defaults if omitted.

Set your Anthropic API key (used for Haiku summarization fallback):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Wire into Claude Code

Run once to register the MCP server:

```bash
claude mcp add cc-orchestrator -- uv run --project /path/to/cc-orchestrator cc-orchestrator
```

Replace `/path/to/cc-orchestrator` with your actual clone path. Restart Claude Code. You now have
four new tools: `dispatch_worker`, `get_run_status`, `get_run_progress`, `get_run_output`.

Alternatively, add a project-level `.mcp.json` at the root of your repo:

```json
{
  "mcpServers": {
    "cc-orchestrator": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/cc-orchestrator",
        "cc-orchestrator"
      ]
    }
  }
}
```

## How dispatch_worker works

`dispatch_worker` returns **immediately** with `{run_id, status: "running"}` — it does not block
until the worker finishes. A background watcher task starts alongside the worker and tails pi's
own session transcript (`~/.pi/agent/sessions/...`) plus `git status`/`git diff --stat` in
`working_dir`, so you can poll for progress instead of waiting out the full `timeout_s` to find
out whether anything happened:

```
dispatch_worker(prompt="Implement story 10.6 per the spec", task_type="implementation",
                working_dir="/path/to/your/project")
# -> {"run_id": "run-20261005-143000-pi", "status": "running"}

get_run_status(run_id="run-20261005-143000-pi")
# -> {"status": "running", "alert": null, "idle_seconds": 12, "files_touched": ["app/foo.py"], ...}
# or, once finished:
# -> {"status": "completed", "claims": {...}, "verification_targets": [...], ...}
```

`alert` is non-null when the watcher's trip-wire has fired — either a repeated same-tool failure
(pi stuck retrying the same thing without making progress) or an idle stall (no transcript
activity for `idle_threshold_s`, process still running). It's a cheap pattern match meant to
prompt you to look closer, not a verdict — check `get_run_output`/the working directory yourself
before deciding to kill a run. `get_run_progress` does everything `get_run_status` does, plus
spends a small Haiku call to turn the raw facts into a one-line human-readable status; poll it
less frequently since it isn't free.

### Parallel dispatch

Since each `dispatch_worker` call spawns an independent `asyncio` subprocess and returns
immediately, issue several calls in the same turn for independent sub-tasks and they run
concurrently — this is the intended pattern for unrelated work, not something that needs
sequential dispatch-and-wait:

```
dispatch_worker(prompt="Implement story 10.6", task_type="implementation", working_dir="...")
dispatch_worker(prompt="Implement story 10.7", task_type="implementation", working_dir="...")
```

Then poll each returned `run_id` independently with `get_run_status`.

### Headless CLI invocation

Pi defaults to an interactive TUI; the config must point at its non-interactive mode or the
subprocess will never exit: `pi` requires `-p`/`--print` in `extra_flags`, or it drops into its
interactive session instead of processing the prompt and exiting. `config.example.yaml` has this
configured correctly as a reference.

## Known Limitations

- **`session="continue"` always falls back to `"new"`**: Worker session IDs are not yet captured
  from CLI output. `session` values outside `{"new", "continue"}` are rejected with an error.
- **Project key is the working directory folder name**: Two projects with the same folder name
  share a session file (`cc_orchestrator/session_store.py`). This is unrelated to how the watcher
  locates pi's own transcript, which uses the full absolute path, mangled per pi's own
  directory-naming scheme (`cc_orchestrator/workers/pi.py::transcript_path`) — don't conflate the
  two if you're extending either.
- **`raw_output_path`/`get_run_output` may be near-empty for pi**: `pi --print` doesn't stream its
  real tool-call activity to stdout; use `get_run_status`/`get_run_progress` instead of treating
  empty output as evidence that nothing happened.
- **Transcript discovery is a heuristic, not a guarantee**: if two `dispatch_worker` calls target
  the same `working_dir` within a few seconds of each other, the watcher may attach to the wrong
  session transcript (it picks the earliest transcript file modified after the dispatch's own
  start time, minus a small clock-skew buffer).

## Contributing

See `config.example.yaml` for `# CONFIGURE THIS` flags. Each worker adapter
in `cc_orchestrator/workers/` is the only CLI-specific code — PRs that add
accurate flags for Pi, or a new worker CLI (including its own
`transcript_path()` if it keeps a session log), are welcome.
```

- [ ] **Step 2: Verify no stale references remain**

Run: `cd /Users/liangguo/My_Skills/cc-orchestrator && grep -ri qoder README.md cc_orchestrator/ config.example.yaml`
Expected: no output (no matches).

- [ ] **Step 3: Commit**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
git add README.md
git commit -m "$(cat <<'EOF'
docs: rewrite README for non-blocking dispatch + watcher architecture

Drops remaining qoder references, documents the four-tool surface
(dispatch_worker/get_run_status/get_run_progress/get_run_output), the
watcher's alert semantics, and parallel-dispatch as the intended
pattern for independent sub-tasks.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
