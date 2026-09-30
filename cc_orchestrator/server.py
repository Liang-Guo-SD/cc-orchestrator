from __future__ import annotations

import datetime
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP as _MCPClass
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCPClass  # type: ignore[assignment]

from cc_orchestrator.config import load_config
from cc_orchestrator.models import Claims, RunResult
from cc_orchestrator.summarizer import summarize
from cc_orchestrator.verifier import generate_targets
from cc_orchestrator.workers.opencode import OpenCodeAdapter
from cc_orchestrator.workers.pi import PiAdapter
from cc_orchestrator.workers.qoder import QoderAdapter

mcp = _MCPClass("cc-orchestrator")


def _base_dir() -> Path:
    return Path.home() / ".cc-orchestrator"


def _make_run_id(agent: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{ts}-{agent}"


def _get_adapter(agent: str, config):
    adapters = {
        "opencode": OpenCodeAdapter,
        "pi": PiAdapter,
        "qoder": QoderAdapter,
    }
    if agent not in adapters:
        return None
    worker_cfg = config.workers.get(agent)
    if worker_cfg is None:
        return None
    runs_dir = _base_dir() / "runs"
    sessions_dir = _base_dir() / "sessions"
    return adapters[agent](worker_cfg, runs_dir, sessions_dir)


def _handle_dispatch(
    agent: str,
    prompt: str,
    session: str,
    working_dir: str,
    timeout_s: int,
    task_type: str,
) -> dict:
    config = load_config()
    adapter = _get_adapter(agent, config)

    if adapter is None:
        return {
            "run_id": "run-error",
            "agent": agent,
            "status": "failed",
            "summary": f"unknown agent '{agent}'; valid agents: opencode, pi, qoder",
            "claims": {"exit_code": 1, "tests_passed": None, "tests_failed": None,
                       "tests_error": None, "files_changed": None, "commit_hash": None,
                       "important_findings": []},
            "verification_targets": [],
            "raw_output_path": "",
            "parent_run_id": None,
        }

    runs_dir = _base_dir() / "runs"
    run_id = _make_run_id(agent)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output_file = run_dir / "output.txt"

    adapter_result = adapter.run(
        prompt=prompt,
        session=session,
        working_dir=working_dir,
        timeout_s=timeout_s,
        output_file=output_file,
    )

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

    targets = generate_targets(claims, working_dir)

    return {
        "run_id": run_id,
        "agent": agent,
        "status": status,
        "summary": summary,
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


def _handle_get_output(run_id: str, lines: int = 100) -> str:
    output_file = _base_dir() / "runs" / run_id / "output.txt"
    if not output_file.exists():
        return f"No output found for run_id '{run_id}'"
    all_lines = output_file.read_text(errors="replace").splitlines()
    return "\n".join(all_lines[-lines:])


@mcp.tool()
def dispatch_worker(
    agent: str,
    prompt: str,
    session: str = "new",
    working_dir: str = ".",
    timeout_s: int = 1800,
    task_type: str = "implementation",
) -> dict:
    """Dispatch a task prompt to a worker CLI agent and return structured results."""
    return _handle_dispatch(agent, prompt, session, working_dir, timeout_s, task_type)


@mcp.tool()
def get_run_output(run_id: str, lines: int = 100) -> str:
    """Fetch the last N lines of raw output from a completed run."""
    return _handle_get_output(run_id, lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
