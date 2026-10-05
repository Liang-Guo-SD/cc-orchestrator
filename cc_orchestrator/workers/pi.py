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
