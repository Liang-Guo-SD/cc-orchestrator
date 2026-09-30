from __future__ import annotations

import json
from pathlib import Path


def _session_path(sessions_dir: Path, agent: str, project_key: str) -> Path:
    agent_dir = sessions_dir / project_key
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir / f"{agent}.json"


def load_session(sessions_dir: Path, agent: str, project_key: str) -> str | None:
    path = _session_path(sessions_dir, agent, project_key)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data.get("session_id")


def save_session(sessions_dir: Path, agent: str, project_key: str, session_id: str) -> None:
    path = _session_path(sessions_dir, agent, project_key)
    path.write_text(json.dumps({"session_id": session_id}))
