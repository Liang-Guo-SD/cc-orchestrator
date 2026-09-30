from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Claims:
    exit_code: int = 0
    tests_passed: int | None = None
    tests_failed: int | None = None
    tests_error: int | None = None
    files_changed: list[str] | None = None
    commit_hash: str | None = None
    important_findings: list[str] = field(default_factory=list)


@dataclass
class VerificationTarget:
    command: str
    expected_pattern: str
    description: str


@dataclass
class RunResult:
    run_id: str
    agent: str
    status: str  # "completed" | "failed" | "timeout" | "low_confidence"
    summary: str
    claims: Claims
    verification_targets: list[VerificationTarget]
    raw_output_path: str
    parent_run_id: str | None = None
