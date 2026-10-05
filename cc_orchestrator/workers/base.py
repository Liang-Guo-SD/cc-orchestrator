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
