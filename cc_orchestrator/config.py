from __future__ import annotations

from dataclasses import dataclass, field
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
    extra_flags: list[str] = None  # type: ignore[assignment]
    command_prefix: list[str] = field(default_factory=list)  # e.g. ["run"] for `opencode run`

    def __post_init__(self) -> None:
        if self.extra_flags is None:
            self.extra_flags = []


@dataclass
class OrchestratorConfig:
    max_retries: int
    workers: dict[str, WorkerConfig]
    haiku_model: str
    anthropic_api_key_env: str
    watch_interval_s: float = 60.0
    idle_threshold_s: float = 300.0
    error_loop_threshold: int = 3


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
            extra_flags=list(entry.get("extra_flags", [])),
            command_prefix=list(entry.get("command_prefix", [])),
        )
        for name, entry in data["workers"].items()
    }
    return OrchestratorConfig(
        max_retries=int(data.get("max_retries", 3)),
        workers=workers,
        haiku_model=data.get("haiku_model", "claude-haiku-4-5-20251001"),
        anthropic_api_key_env=data.get("anthropic_api_key_env", "ANTHROPIC_API_KEY"),
        watch_interval_s=float(data.get("watch_interval_s", 60.0)),
        idle_threshold_s=float(data.get("idle_threshold_s", 300.0)),
        error_loop_threshold=int(data.get("error_loop_threshold", 3)),
    )
