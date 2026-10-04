from __future__ import annotations

import asyncio
from pathlib import Path

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.session_store import load_session, save_session
from cc_orchestrator.workers.base import AdapterResult, WorkerAdapter


class OpenCodeAdapter(WorkerAdapter):
    _agent_name: str = "opencode"

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
        return {}  # OpenCode uses standard tool outputs; built-ins cover it

    async def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
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
