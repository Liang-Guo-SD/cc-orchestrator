from __future__ import annotations

import re
from cc_orchestrator.models import Claims

# Built-in patterns applied to every run regardless of adapter.
_BUILTIN_PATTERNS: dict[str, str] = {
    "tests_passed":  r"(\d+) passed",
    "tests_failed":  r"(\d+) failed",
    "tests_error":   r"(\d+) error",
    "commit_hash":   r"\[[\w/.-]+ ([0-9a-f]{7,})\]",
    "files_changed": r"(\d+) files? changed",
}

# Expected fields per task_type for confidence scoring.
_EXPECTED_FIELDS: dict[str, list[str]] = {
    "verification":     ["tests_passed", "tests_failed", "tests_error"],
    "implementation":   ["tests_passed", "tests_failed"],
    "close_out":        ["tests_passed", "tests_failed", "commit_hash", "files_changed"],
    "code_review":      [],  # always 0 → Haiku always used
}


def extract_claims_rule_based(
    text: str,
    exit_code: int,
    patterns: dict[str, str],
) -> Claims:
    """Apply built-in + adapter-supplied patterns to raw output text.

    `patterns` comes from WorkerAdapter.extraction_patterns() and may
    override or extend the built-ins.
    """
    all_patterns = {**_BUILTIN_PATTERNS, **patterns}
    raw: dict[str, str] = {}
    for field, pattern in all_patterns.items():
        m = re.search(pattern, text)
        if m:
            raw[field] = m.group(1)

    files_changed: list[str] | None = None
    if "files_changed" in raw:
        # We only know the COUNT from git output; synthesise a list of that length.
        count = int(raw["files_changed"])
        files_changed = [f"<file {i+1}>" for i in range(count)]

    return Claims(
        exit_code=exit_code,
        tests_passed=int(raw["tests_passed"]) if "tests_passed" in raw else None,
        tests_failed=int(raw["tests_failed"]) if "tests_failed" in raw else None,
        tests_error=int(raw["tests_error"]) if "tests_error" in raw else None,
        commit_hash=raw.get("commit_hash"),
        files_changed=files_changed,
    )


def compute_confidence(claims: Claims, task_type: str) -> float:
    expected = _EXPECTED_FIELDS.get(task_type, [])
    if not expected:
        return 0.0
    populated = sum(
        1 for f in expected if getattr(claims, f, None) is not None
    )
    return populated / len(expected)
