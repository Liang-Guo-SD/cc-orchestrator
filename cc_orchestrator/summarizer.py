from __future__ import annotations

import json
import re
import anthropic
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


_HAIKU_PROMPT = """\
This is the tail of a software engineering task's terminal output.
Extract as JSON with exactly these keys:
"summary" (one sentence string),
"tests_passed" (integer or null),
"tests_failed" (integer or null),
"files_changed" (list of strings or null),
"commit_hash" (string or null),
"important_findings" (list of up to 3 strings).
Respond with only the JSON object, no markdown fences.

Output:
{tail}"""


def summarize_with_haiku(
    text_tail: str,
    haiku_model: str,
    api_key: str,
) -> dict:
    """Call Haiku and return parsed JSON dict. Raises on API error."""
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=haiku_model,
        max_tokens=512,
        messages=[{"role": "user", "content": _HAIKU_PROMPT.format(tail=text_tail)}],
    )
    return json.loads(msg.content[0].text)


def summarize(
    raw_text: str,
    exit_code: int,
    task_type: str,
    adapter_patterns: dict[str, str],
    haiku_model: str,
    api_key: str | None,
) -> tuple[Claims, str, float]:
    """Full hybrid pipeline. Returns (claims, summary_sentence, confidence)."""
    claims = extract_claims_rule_based(raw_text, exit_code, adapter_patterns)
    confidence = compute_confidence(claims, task_type)

    if confidence >= 0.6:
        # High confidence: skip Haiku; generate terse prose summary locally.
        parts = []
        if claims.tests_passed is not None:
            parts.append(f"{claims.tests_passed} passed")
        if claims.tests_failed:
            parts.append(f"{claims.tests_failed} failed")
        if claims.commit_hash:
            parts.append(f"commit {claims.commit_hash}")
        summary = "; ".join(parts) if parts else "completed"
        return claims, summary, confidence

    # Low confidence: try Haiku.
    if api_key is None:
        return claims, "", confidence

    tail = "\n".join(raw_text.splitlines()[-200:])
    try:
        haiku_data = summarize_with_haiku(tail, haiku_model, api_key)
    except (anthropic.AuthenticationError, anthropic.APIError, json.JSONDecodeError):
        return claims, "", confidence

    # Merge: rule-based fields are authoritative; Haiku fills nulls.
    if claims.tests_passed is None and haiku_data.get("tests_passed") is not None:
        claims.tests_passed = haiku_data["tests_passed"]
    if claims.tests_failed is None and haiku_data.get("tests_failed") is not None:
        claims.tests_failed = haiku_data["tests_failed"]
    if claims.files_changed is None and haiku_data.get("files_changed"):
        claims.files_changed = haiku_data["files_changed"]
    if claims.commit_hash is None and haiku_data.get("commit_hash"):
        claims.commit_hash = haiku_data["commit_hash"]
    if not claims.important_findings:
        claims.important_findings = haiku_data.get("important_findings") or []

    summary = haiku_data.get("summary", "")
    confidence = compute_confidence(claims, task_type)
    return claims, summary, confidence
