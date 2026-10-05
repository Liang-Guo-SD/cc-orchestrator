from __future__ import annotations

import asyncio
import datetime
import json
import os
import uuid
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
    suffix = uuid.uuid4().hex[:6]
    return f"run-{ts}-{agent}-{suffix}"


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
