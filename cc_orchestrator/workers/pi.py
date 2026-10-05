from __future__ import annotations

import datetime
import re
from pathlib import Path

from cc_orchestrator.workers.cli_adapter import CliAdapter

# pi's session filenames start with their own creation timestamp:
# "2026-10-05T12-45-52-117Z_<uuid>.jsonl". This is the file's creation
# time as pi itself recorded it -- deliberately not the filesystem mtime,
# which keeps growing as pi appends to the file throughout the run and so
# cannot distinguish "created after our dispatch" from "still being
# written to by an earlier, unrelated run".
_FILENAME_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z_"
)


def _parse_filename_timestamp(path: Path) -> datetime.datetime | None:
    match = _FILENAME_TIMESTAMP_RE.match(path.name)
    if not match:
        return None
    year, month, day, hour, minute, second, millis = (int(g) for g in match.groups())
    return datetime.datetime(
        year, month, day, hour, minute, second, millis * 1000,
        tzinfo=datetime.timezone.utc,
    )


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

        Among files whose own filename-encoded creation timestamp is at or
        after `started_after` (minus a small clock-skew buffer), returns
        the earliest one -- the file closest to our own dispatch's start
        time. Files whose name doesn't match pi's timestamp convention are
        skipped as not recognizably pi's own transcripts. If two dispatches
        start within the buffer window on the same working_dir, this can
        still pick the wrong one; that remains a known heuristic
        limitation, not a correctness guarantee.
        """
        abs_path = str(Path(working_dir).resolve())
        mangled = "-" + abs_path.replace("/", "-") + "--"
        session_dir = Path.home() / ".pi" / "agent" / "sessions" / mangled
        if not session_dir.exists():
            return None

        cutoff = started_after - datetime.timedelta(seconds=5)  # buffer for clock skew
        dated_candidates: list[tuple[datetime.datetime, Path]] = []
        for p in session_dir.glob("*.jsonl"):
            created_at = _parse_filename_timestamp(p)
            if created_at is not None and created_at >= cutoff:
                dated_candidates.append((created_at, p))

        if not dated_candidates:
            return None
        dated_candidates.sort(key=lambda pair: pair[0])
        return dated_candidates[0][1]
