from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigNotFoundError(FileNotFoundError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"cc-orchestrator config not found: {path}")


@dataclass
class WorkerConfig:
    bin: str
    new_session_flag: str
    continue_flag: str
    session_id_flag: str


@dataclass
class OrchestratorConfig:
    max_retries: int
    workers: dict[str, WorkerConfig]
    haiku_model: str
    anthropic_api_key_env: str


def load_config(path: Path | None = None) -> OrchestratorConfig:
    if path is None:
        path = Path.home() / ".cc-orchestrator" / "config.yaml"
    if not path.exists():
        raise ConfigNotFoundError(path)
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    workers = {
        name: WorkerConfig(
            bin=entry["bin"],
            new_session_flag=entry.get("new_session_flag", ""),
            continue_flag=entry.get("continue_flag", ""),
            session_id_flag=entry.get("session_id_flag", ""),
        )
        for name, entry in data["workers"].items()
    }
    return OrchestratorConfig(
        max_retries=int(data.get("max_retries", 3)),
        workers=workers,
        haiku_model=data.get("haiku_model", "claude-haiku-4-5-20251001"),
        anthropic_api_key_env=data.get("anthropic_api_key_env", "ANTHROPIC_API_KEY"),
    )
