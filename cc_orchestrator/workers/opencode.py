from __future__ import annotations

import subprocess
from pathlib import Path

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.session_store import load_session, save_session
from cc_orchestrator.workers.base import AdapterResult, WorkerAdapter


class OpenCodeAdapter(WorkerAdapter):
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

    def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
    ) -> AdapterResult:
        project_key = Path(working_dir).name
        cmd = [self._cfg.bin]

        if session == "continue":
            stored = load_session(self._sessions_dir, "opencode", project_key)
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
            with output_file.open("w") as out_fh:
                result = subprocess.run(
                    cmd,
                    stdout=out_fh,
                    stderr=subprocess.STDOUT,
                    cwd=working_dir,
                    timeout=timeout_s,
                )
            return AdapterResult(exit_code=result.returncode, session_id=None)
        except FileNotFoundError:
            output_file.write_text(f"error: binary '{self._cfg.bin}' not found on PATH\n")
            return AdapterResult(exit_code=127, session_id=None)
        except subprocess.TimeoutExpired:
            return AdapterResult(exit_code=124, session_id=None)
