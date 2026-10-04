from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


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
    ) -> AdapterResult:
        """Spawn the worker CLI, write all output to output_file, return exit code.

        Must use asyncio subprocess (not subprocess.run) so concurrent
        dispatch_worker calls can run multiple workers in parallel on one
        event loop.
        """
        ...

    @abstractmethod
    def extraction_patterns(self) -> dict[str, str]:
        """Return regex patterns this adapter knows how to extract.
        Keys match Claims field names. Values are regex strings with one capture group."""
        ...
