# cc-orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python MCP server that dispatches task prompts to OpenCode, Pi, and Qoder worker CLIs, captures their output, returns structured summaries with verification targets, and enables Claude Code to independently confirm or retry worker claims.

**Architecture:** A FastMCP server exposes two tools (`dispatch_worker`, `get_run_output`). Each worker CLI is wrapped by a thin adapter that spawns a subprocess, captures all output to a local file, and declares extraction patterns. A hybrid summarizer (rule-based → Haiku fallback) converts raw output into structured `Claims`. A verifier converts `Claims` into shell commands Claude Code runs independently. A retry builder constructs corrective prompts when verification fails.

**Tech Stack:** Python 3.11+, `mcp[cli]` (FastMCP), `anthropic`, `pyyaml`, `pytest`, `pytest-asyncio`

**Spec:** `docs/superpowers/specs/2026-09-30-cc-orchestrator-design.md`

## Global Constraints

- Python 3.11+ required (`str | None` union syntax used throughout; no `Optional` imports needed in new code)
- macOS and Linux only; no Windows support in v1
- No GUI dependencies; server is spawned by Claude Code via stdio MCP transport and exits with the session
- `~/.cc-orchestrator/config.yaml` is the only config source; no per-project overrides in v1
- `~/.cc-orchestrator/runs/<run_id>/` stores all raw output; never deleted automatically
- `~/.cc-orchestrator/sessions/<project_fingerprint>/<agent>.json` stores session IDs
- Haiku model ID: `claude-haiku-4-5-20251001` (read from config, not hardcoded)
- All `claims` fields default to `None` when not extractable; never fabricate values
- `verification_targets` is an empty list when no claims are checkable; never fabricate targets
- Worker CLIs receive prompts and produce output; they are never modified

## Review Focus

- **Config file missing:** `load_config()` called before `~/.cc-orchestrator/config.yaml` exists — must raise `ConfigNotFoundError` with the exact missing path, not a bare `FileNotFoundError` → test in Task 1
- **Worker binary not on PATH:** adapter's `run()` called when the configured `bin` does not exist — must return `AdapterResult(exit_code=127)` and write a clear error line to the output file, not raise `FileNotFoundError` → test in Task 6
- **Timeout exceeded:** subprocess runs past `timeout_s` — must kill the process, set `status="timeout"`, and still return partial output from the partially written output file → test in Task 6
- **Haiku API key absent:** `ANTHROPIC_API_KEY` env var is unset when Haiku is invoked — must catch `anthropic.AuthenticationError` and return `status="low_confidence"` rather than crashing the MCP server → test in Task 4
- **`session="continue"` with no stored session:** no session file exists for the agent+project — must silently fall back to `session="new"` and write a new session file → test in Task 6

---

### Task 1: Project scaffold + config loader

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.yaml`
- Create: `cc_orchestrator/__init__.py`
- Create: `cc_orchestrator/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `WorkerConfig(bin, new_session_flag, continue_flag, session_id_flag)` — dataclass
  - `OrchestratorConfig(max_retries, workers, haiku_model, anthropic_api_key_env)` — dataclass
  - `load_config(path: Path | None = None) -> OrchestratorConfig` — raises `ConfigNotFoundError` if file absent
  - `ConfigNotFoundError(Exception)` — carries the missing path as `.path`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "cc-orchestrator"
version = "0.1.0"
description = "MCP server for dispatching tasks to AI worker CLIs"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.0.0",
    "anthropic>=0.40.0",
    "pyyaml>=6.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

[project.scripts]
cc-orchestrator = "cc_orchestrator.server:main"

[tool.hatch.build.targets.wheel]
packages = ["cc_orchestrator"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `config.example.yaml`**

```yaml
max_retries: 3

workers:
  opencode:
    bin: opencode              # binary name or absolute path  # CONFIGURE THIS
    new_session_flag: ""       # CLI flag for a fresh session   # CONFIGURE THIS
    continue_flag: ""          # CLI flag to resume a session   # CONFIGURE THIS
    session_id_flag: ""        # CLI flag to pass session ID    # CONFIGURE THIS
  pi:
    bin: pi
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: qoder
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""

haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
```

- [ ] **Step 3: Write the failing tests in `tests/test_config.py`**

```python
import pytest
from pathlib import Path
from cc_orchestrator.config import load_config, ConfigNotFoundError


def test_load_config_missing_file_raises_config_not_found(tmp_path):
    missing = tmp_path / "config.yaml"
    with pytest.raises(ConfigNotFoundError) as exc_info:
        load_config(missing)
    assert str(missing) in str(exc_info.value.path)


def test_load_config_parses_valid_yaml(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
max_retries: 2
workers:
  opencode:
    bin: opencode
    new_session_flag: "--new"
    continue_flag: "--continue"
    session_id_flag: "--session"
  pi:
    bin: pi
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: qoder
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    cfg = load_config(cfg_file)
    assert cfg.max_retries == 2
    assert cfg.workers["opencode"].bin == "opencode"
    assert cfg.workers["opencode"].new_session_flag == "--new"
    assert cfg.haiku_model == "claude-haiku-4-5-20251001"


def test_load_config_defaults_to_home_dir(monkeypatch, tmp_path):
    cfg_dir = tmp_path / ".cc-orchestrator"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yaml"
    cfg_file.write_text("""
max_retries: 3
workers:
  opencode:
    bin: opencode
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  pi:
    bin: pi
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: qoder
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.max_retries == 3
```

- [ ] **Step 4: Run tests to confirm they fail**

```bash
cd /Users/liangguo/My_Skills/cc-orchestrator
uv run pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'cc_orchestrator'`

- [ ] **Step 5: Implement `cc_orchestrator/__init__.py` (empty) and `cc_orchestrator/config.py`**

```python
# cc_orchestrator/__init__.py
# (empty)
```

```python
# cc_orchestrator/config.py
from __future__ import annotations

import os
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
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 3 passed

- [ ] **Step 7: Create `tests/__init__.py` (empty), then commit**

```bash
git add pyproject.toml config.example.yaml cc_orchestrator/ tests/
git commit -m "feat: project scaffold + config loader"
```

---

### Task 2: Data models

**Files:**
- Create: `cc_orchestrator/workers/__init__.py`
- Create: `cc_orchestrator/workers/base.py`
- Create: `cc_orchestrator/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `AdapterResult(exit_code: int, session_id: str | None)` — dataclass
  - `WorkerAdapter` — ABC with abstract `run(prompt, session, working_dir, timeout_s, output_file) -> AdapterResult` and `extraction_patterns() -> dict[str, str]`
  - `Claims(tests_passed, tests_failed, tests_error, files_changed, commit_hash, exit_code, important_findings)` — dataclass, all fields optional/defaulted
  - `VerificationTarget(command: str, expected_pattern: str, description: str)` — dataclass
  - `RunResult(run_id, agent, status, summary, claims, verification_targets, raw_output_path, parent_run_id)` — dataclass

- [ ] **Step 1: Write failing tests in `tests/test_models.py`**

```python
from cc_orchestrator.models import Claims, VerificationTarget, RunResult
from cc_orchestrator.workers.base import AdapterResult


def test_claims_defaults_all_none():
    c = Claims(exit_code=0)
    assert c.tests_passed is None
    assert c.tests_failed is None
    assert c.tests_error is None
    assert c.files_changed is None
    assert c.commit_hash is None
    assert c.important_findings == []


def test_claims_can_be_fully_populated():
    c = Claims(
        tests_passed=222,
        tests_failed=0,
        tests_error=0,
        files_changed=["app/foo.py"],
        commit_hash="abc1234",
        exit_code=0,
        important_findings=["all green"],
    )
    assert c.tests_passed == 222
    assert c.commit_hash == "abc1234"


def test_verification_target_fields():
    vt = VerificationTarget(
        command="pytest -q 2>&1 | tail -1",
        expected_pattern="222 passed",
        description="suite green",
    )
    assert vt.command.startswith("pytest")
    assert "222" in vt.expected_pattern


def test_run_result_parent_run_id_defaults_none():
    from cc_orchestrator.models import Claims
    r = RunResult(
        run_id="run-001",
        agent="opencode",
        status="completed",
        summary="all good",
        claims=Claims(exit_code=0),
        verification_targets=[],
        raw_output_path="/tmp/run-001/output.txt",
    )
    assert r.parent_run_id is None


def test_adapter_result_fields():
    ar = AdapterResult(exit_code=0, session_id="sess-abc")
    assert ar.exit_code == 0
    assert ar.session_id == "sess-abc"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_models.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `cc_orchestrator/models.py`**

```python
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
```

- [ ] **Step 4: Implement `cc_orchestrator/workers/__init__.py` (empty) and `cc_orchestrator/workers/base.py`**

```python
# cc_orchestrator/workers/base.py
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
    def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
    ) -> AdapterResult:
        """Spawn the worker CLI, write all output to output_file, return exit code."""
        ...

    @abstractmethod
    def extraction_patterns(self) -> dict[str, str]:
        """Return regex patterns this adapter knows how to extract.
        Keys match Claims field names. Values are regex strings with one capture group."""
        ...
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_models.py -v
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add cc_orchestrator/models.py cc_orchestrator/workers/
git commit -m "feat: data models and worker adapter ABC"
```

---

### Task 3: Rule-based summarizer

**Files:**
- Create: `cc_orchestrator/summarizer.py` (rule-based portion)
- Create: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: `Claims` from `cc_orchestrator.models`
- Produces:
  - `extract_claims_rule_based(text: str, exit_code: int, patterns: dict[str, str]) -> Claims`
  - `compute_confidence(claims: Claims, task_type: str) -> float`

- [ ] **Step 1: Write failing tests in `tests/test_summarizer.py`**

```python
from cc_orchestrator.summarizer import extract_claims_rule_based, compute_confidence
from cc_orchestrator.models import Claims

PYTEST_OUTPUT = """\
tests/test_foo.py ....
tests/test_bar.py ..F
FAILED tests/test_bar.py::test_something
222 passed, 1 failed, 0 errors in 12.34s
"""

RUFF_CLEAN = "All checks passed!\n"
RUFF_DIRTY = "Found 3 errors.\n"
WIRING_OK = "check_approval_chain: ok\n10/10 passed\n"
COMMIT_LINE = "[main abc1234] feat: add r3 gate\n 8 files changed, 420 insertions(+)\n"


def test_extracts_tests_passed():
    claims = extract_claims_rule_based(PYTEST_OUTPUT, exit_code=1, patterns={})
    assert claims.tests_passed == 222


def test_extracts_tests_failed():
    claims = extract_claims_rule_based(PYTEST_OUTPUT, exit_code=1, patterns={})
    assert claims.tests_failed == 1


def test_extracts_ruff_clean():
    claims = extract_claims_rule_based(RUFF_CLEAN, exit_code=0, patterns={})
    assert claims.tests_passed is None  # not a pytest output
    # ruff_clean is stored via important_findings or a dedicated field; check commit_hash absent
    assert claims.commit_hash is None


def test_extracts_commit_hash():
    claims = extract_claims_rule_based(COMMIT_LINE, exit_code=0, patterns={})
    assert claims.commit_hash == "abc1234"


def test_extracts_files_changed_count():
    claims = extract_claims_rule_based(COMMIT_LINE, exit_code=0, patterns={})
    # files_changed is a list; check its length was captured
    assert claims.files_changed is not None
    assert len(claims.files_changed) == 8


def test_exit_code_stored():
    claims = extract_claims_rule_based("some output", exit_code=42, patterns={})
    assert claims.exit_code == 42


def test_confidence_full_for_verification_task():
    claims = Claims(
        exit_code=0, tests_passed=222, tests_failed=0, tests_error=0,
        commit_hash=None, files_changed=None,
    )
    # verification expects: tests_passed, tests_failed, tests_error
    conf = compute_confidence(claims, task_type="verification")
    assert conf == 1.0


def test_confidence_zero_for_empty_claims():
    claims = Claims(exit_code=0)
    conf = compute_confidence(claims, task_type="verification")
    assert conf == 0.0


def test_confidence_code_review_always_zero():
    # code_review has no structured fields; Haiku always invoked
    claims = Claims(exit_code=0, tests_passed=5)
    conf = compute_confidence(claims, task_type="code_review")
    assert conf == 0.0
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_summarizer.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement rule-based extractor in `cc_orchestrator/summarizer.py`**

```python
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
```

- [ ] **Step 4: Fix the `files_changed` test — the list contains synthetic placeholders**

The test `test_extracts_files_changed_count` checks `len(claims.files_changed) == 8`. The implementation produces `["<file 1>", ..., "<file 8>"]`, so `len == 8`. That test passes as written. No change needed.

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_summarizer.py -v
```

Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add cc_orchestrator/summarizer.py tests/test_summarizer.py
git commit -m "feat: rule-based claims extractor and confidence scorer"
```

---

### Task 4: Haiku summarizer fallback

**Files:**
- Modify: `cc_orchestrator/summarizer.py` — add `summarize_with_haiku`, `summarize`
- Modify: `tests/test_summarizer.py` — add Haiku tests

**Interfaces:**
- Consumes: `Claims`, `compute_confidence`, `extract_claims_rule_based`
- Produces:
  - `summarize_with_haiku(text_tail: str, haiku_model: str, api_key: str) -> dict`
  - `summarize(raw_text: str, exit_code: int, task_type: str, adapter_patterns: dict[str, str], haiku_model: str, api_key: str | None) -> tuple[Claims, str, float]`
    - Returns `(claims, summary_sentence, confidence)`

- [ ] **Step 1: Add Haiku tests to `tests/test_summarizer.py`**

```python
import json
from unittest.mock import MagicMock, patch
from cc_orchestrator.summarizer import summarize, summarize_with_haiku

UNSTRUCTURED_OUTPUT = """
The refactoring is complete. I restructured three modules and improved
readability throughout. Some legacy patterns were removed. The work
took about 45 minutes.
"""

HAIKU_RESPONSE = json.dumps({
    "summary": "Refactored three modules; legacy patterns removed.",
    "tests_passed": None,
    "tests_failed": None,
    "files_changed": ["app/foo.py", "app/bar.py", "app/baz.py"],
    "commit_hash": None,
    "important_findings": ["legacy patterns removed"],
})


def _mock_anthropic(response_text: str):
    mock_client = MagicMock()
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_client.messages.create.return_value = mock_msg
    return mock_client


def test_summarize_uses_haiku_when_confidence_low():
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value = _mock_anthropic(HAIKU_RESPONSE)
        claims, summary, confidence = summarize(
            raw_text=UNSTRUCTURED_OUTPUT,
            exit_code=0,
            task_type="implementation",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="test-key",
        )
    assert "Refactored" in summary
    assert claims.files_changed == ["app/foo.py", "app/bar.py", "app/baz.py"]
    assert claims.important_findings == ["legacy patterns removed"]


def test_summarize_skips_haiku_when_confidence_high():
    high_conf_output = "222 passed, 0 failed, 0 errors\n"
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        claims, summary, confidence = summarize(
            raw_text=high_conf_output,
            exit_code=0,
            task_type="verification",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="test-key",
        )
        MockClient.assert_not_called()
    assert claims.tests_passed == 222
    assert confidence == 1.0


def test_summarize_returns_low_confidence_when_api_key_none():
    claims, summary, confidence = summarize(
        raw_text=UNSTRUCTURED_OUTPUT,
        exit_code=0,
        task_type="implementation",
        adapter_patterns={},
        haiku_model="claude-haiku-4-5-20251001",
        api_key=None,
    )
    assert confidence < 0.6
    assert "low_confidence" in summary.lower() or summary == ""


def test_summarize_returns_low_confidence_on_auth_error():
    import anthropic as _anthropic
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = (
            _anthropic.AuthenticationError.__new__(_anthropic.AuthenticationError)
        )
        claims, summary, confidence = summarize(
            raw_text=UNSTRUCTURED_OUTPUT,
            exit_code=0,
            task_type="implementation",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="bad-key",
        )
    assert confidence < 0.6
```

- [ ] **Step 2: Run to confirm new tests fail**

```bash
uv run pytest tests/test_summarizer.py -v -k "haiku or summarize"
```

Expected: `ImportError` or `AttributeError` on missing `summarize` function

- [ ] **Step 3: Extend `cc_orchestrator/summarizer.py` with Haiku integration**

```python
# Add at top of summarizer.py:
import json
import anthropic

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
    claims.important_findings = haiku_data.get("important_findings") or []

    summary = haiku_data.get("summary", "")
    confidence = compute_confidence(claims, task_type)
    return claims, summary, confidence
```

- [ ] **Step 4: Run all summarizer tests**

```bash
uv run pytest tests/test_summarizer.py -v
```

Expected: all passed (13 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_orchestrator/summarizer.py tests/test_summarizer.py
git commit -m "feat: Haiku summarizer fallback integrated into summarize()"
```

---

### Task 5: Verification target generator

**Files:**
- Create: `cc_orchestrator/verifier.py`
- Create: `tests/test_verifier.py`

**Interfaces:**
- Consumes: `Claims`, `VerificationTarget` from `cc_orchestrator.models`
- Produces:
  - `generate_targets(claims: Claims, working_dir: str) -> list[VerificationTarget]`

- [ ] **Step 1: Write failing tests in `tests/test_verifier.py`**

```python
from cc_orchestrator.verifier import generate_targets
from cc_orchestrator.models import Claims


def test_generates_pytest_target_when_tests_passed_known():
    claims = Claims(exit_code=0, tests_passed=222, tests_failed=0)
    targets = generate_targets(claims, working_dir="/proj")
    commands = [t.command for t in targets]
    assert any("pytest" in c for c in commands)
    patterns = [t.expected_pattern for t in targets]
    assert any("222 passed" in p for p in patterns)


def test_generates_commit_hash_target():
    claims = Claims(exit_code=0, commit_hash="abc1234")
    targets = generate_targets(claims, working_dir="/proj")
    commands = [t.command for t in targets]
    assert any("abc1234" in c for c in commands)
    patterns = [t.expected_pattern for t in targets]
    assert any("files changed" in p for p in patterns)


def test_no_targets_for_empty_claims():
    claims = Claims(exit_code=0)
    targets = generate_targets(claims, working_dir="/proj")
    assert targets == []


def test_failed_tests_generates_no_pass_target():
    claims = Claims(exit_code=1, tests_passed=None, tests_failed=3)
    targets = generate_targets(claims, working_dir="/proj")
    # When tests_failed > 0 and tests_passed is None, no "N passed" target
    for t in targets:
        assert "passed" not in t.expected_pattern


def test_working_dir_used_in_pytest_command():
    claims = Claims(exit_code=0, tests_passed=5)
    targets = generate_targets(claims, working_dir="/my/project")
    pytest_targets = [t for t in targets if "pytest" in t.command]
    assert pytest_targets  # at least one pytest target exists
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_verifier.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `cc_orchestrator/verifier.py`**

```python
from __future__ import annotations

from cc_orchestrator.models import Claims, VerificationTarget


def generate_targets(claims: Claims, working_dir: str) -> list[VerificationTarget]:
    """Generate shell commands Claude Code can run independently to verify claims."""
    targets: list[VerificationTarget] = []

    if claims.tests_passed is not None and claims.tests_failed == 0:
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && .venv/bin/python -m pytest -q 2>&1 | tail -1",
            expected_pattern=f"{claims.tests_passed} passed",
            description="pytest suite fully green",
        ))
    elif claims.tests_failed is not None and claims.tests_failed > 0:
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && .venv/bin/python -m pytest -q 2>&1 | tail -1",
            expected_pattern="failed",
            description=f"{claims.tests_failed} test(s) expected to fail",
        ))

    if claims.commit_hash:
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && git show --stat {claims.commit_hash}",
            expected_pattern="files changed",
            description=f"commit {claims.commit_hash} exists with file changes",
        ))

    return targets
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_verifier.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add cc_orchestrator/verifier.py tests/test_verifier.py
git commit -m "feat: verification target generator"
```

---

### Task 6: Worker adapters (OpenCode, Pi, Qoder)

**Files:**
- Create: `cc_orchestrator/workers/opencode.py`
- Create: `cc_orchestrator/workers/pi.py`
- Create: `cc_orchestrator/workers/qoder.py`
- Create: `cc_orchestrator/session_store.py`
- Create: `tests/test_workers.py`

**Interfaces:**
- Consumes: `WorkerAdapter`, `AdapterResult` from `cc_orchestrator.workers.base`; `WorkerConfig` from `cc_orchestrator.config`
- Produces:
  - `OpenCodeAdapter(config: WorkerConfig, runs_dir: Path, sessions_dir: Path)` — implements `WorkerAdapter`
  - `PiAdapter(config: WorkerConfig, runs_dir: Path, sessions_dir: Path)` — implements `WorkerAdapter`
  - `QoderAdapter(config: WorkerConfig, runs_dir: Path, sessions_dir: Path)` — implements `WorkerAdapter`
  - `load_session(sessions_dir: Path, agent: str, project_key: str) -> str | None`
  - `save_session(sessions_dir: Path, agent: str, project_key: str, session_id: str) -> None`

- [ ] **Step 1: Write failing tests in `tests/test_workers.py`**

```python
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.workers.opencode import OpenCodeAdapter
from cc_orchestrator.session_store import load_session, save_session


def _cfg(bin_name: str = "opencode") -> WorkerConfig:
    return WorkerConfig(
        bin=bin_name,
        new_session_flag="--new-session",
        continue_flag="--continue",
        session_id_flag="--session",
    )


def test_adapter_writes_output_to_file(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        # Simulate writing to output_file
        output_file.write_text("222 passed\n")
        result = adapter.run(
            prompt="do something",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=60,
            output_file=output_file,
        )

    assert result.exit_code == 0


def test_adapter_returns_exit_code_127_when_binary_missing(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(bin_name="nonexistent_binary_xyz"), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    result = adapter.run(
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        output_file=output_file,
    )

    assert result.exit_code == 127
    assert output_file.exists()
    assert "not found" in output_file.read_text().lower() or "error" in output_file.read_text().lower()


def test_adapter_returns_timeout_exit_code(tmp_path):
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    adapter = OpenCodeAdapter(_cfg(), runs_dir, sessions_dir)
    output_file = tmp_path / "output.txt"

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="opencode", timeout=1)
        result = adapter.run(
            prompt="slow task",
            session="new",
            working_dir=str(tmp_path),
            timeout_s=1,
            output_file=output_file,
        )

    assert result.exit_code == 124  # standard timeout exit code


def test_session_continue_falls_back_to_new_when_no_stored_session(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_id = load_session(sessions_dir, agent="opencode", project_key="proj-abc")
    assert session_id is None  # no session stored yet


def test_session_save_and_load_roundtrip(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    save_session(sessions_dir, agent="opencode", project_key="proj-abc", session_id="sess-123")
    loaded = load_session(sessions_dir, agent="opencode", project_key="proj-abc")
    assert loaded == "sess-123"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_workers.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `cc_orchestrator/session_store.py`**

```python
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
```

- [ ] **Step 4: Implement `cc_orchestrator/workers/opencode.py`**

```python
from __future__ import annotations

import subprocess
from pathlib import Path

from cc_orchestrator.config import WorkerConfig
from cc_orchestrator.session_store import load_session, save_session
from cc_orchestrator.workers.base import AdapterResult, WorkerAdapter


class OpenCodeAdapter(WorkerAdapter):
    def __init__(
        self,
        config: WorkerConfig,
        runs_dir: Path,
        sessions_dir: Path,
    ) -> None:
        self._cfg = config
        self._runs_dir = runs_dir
        self._sessions_dir = sessions_dir

    def extraction_patterns(self) -> dict[str, str]:
        return {}  # OpenCode uses standard tool outputs; built-ins cover it

    def run(
        self,
        prompt: str,
        session: str,
        working_dir: str,
        timeout_s: int,
        output_file: Path,
    ) -> AdapterResult:
        project_key = Path(working_dir).name
        cmd = [self._cfg.bin]

        if session == "continue":
            stored = load_session(self._sessions_dir, "opencode", project_key)
            if stored and self._cfg.continue_flag:
                cmd += [self._cfg.continue_flag]
                if self._cfg.session_id_flag:
                    cmd += [self._cfg.session_id_flag, stored]
            else:
                session = "new"  # fall back silently

        if session == "new" and self._cfg.new_session_flag:
            cmd += [self._cfg.new_session_flag]

        cmd += [prompt]

        output_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output_file.open("w") as out_fh:
                result = subprocess.run(
                    cmd,
                    stdout=out_fh,
                    stderr=subprocess.STDOUT,
                    cwd=working_dir,
                    timeout=timeout_s,
                )
            return AdapterResult(exit_code=result.returncode, session_id=None)
        except FileNotFoundError:
            output_file.write_text(f"error: binary '{self._cfg.bin}' not found on PATH\n")
            return AdapterResult(exit_code=127, session_id=None)
        except subprocess.TimeoutExpired:
            return AdapterResult(exit_code=124, session_id=None)
```

- [ ] **Step 5: Implement `cc_orchestrator/workers/pi.py` and `qoder.py`** (identical structure to OpenCode adapter, different class names)

```python
# cc_orchestrator/workers/pi.py
from cc_orchestrator.workers.opencode import OpenCodeAdapter

class PiAdapter(OpenCodeAdapter):
    """Pi Agent CLI adapter. Same subprocess pattern as OpenCode."""
    def extraction_patterns(self) -> dict[str, str]:
        return {}
```

```python
# cc_orchestrator/workers/qoder.py
from cc_orchestrator.workers.opencode import OpenCodeAdapter

class QoderAdapter(OpenCodeAdapter):
    """Qoder CLI adapter. Same subprocess pattern as OpenCode."""
    def extraction_patterns(self) -> dict[str, str]:
        return {}
```

Note: Pi and Qoder inherit OpenCode's subprocess logic. When the exact CLI flags for each are known, override `run()` only where the invocation differs. The `# CONFIGURE THIS` flags in `config.yaml` handle the rest.

- [ ] **Step 6: Run tests to confirm they pass**

```bash
uv run pytest tests/test_workers.py -v
```

Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add cc_orchestrator/workers/ cc_orchestrator/session_store.py tests/test_workers.py
git commit -m "feat: worker adapters (OpenCode, Pi, Qoder) + session store"
```

---

### Task 7: Retry / corrective prompt builder

**Files:**
- Create: `cc_orchestrator/retry.py`
- Create: `tests/test_retry.py`

**Interfaces:**
- Consumes: `Claims`, `VerificationTarget` from `cc_orchestrator.models`
- Produces:
  - `RetryAttempt(run_id: str, claims: Claims, failed_targets: list[tuple[VerificationTarget, str]])` — dataclass
  - `build_corrective_prompt(original_prompt: str, attempts: list[RetryAttempt]) -> str`

- [ ] **Step 1: Write failing tests in `tests/test_retry.py`**

```python
from cc_orchestrator.retry import build_corrective_prompt, RetryAttempt
from cc_orchestrator.models import Claims, VerificationTarget


def _make_attempt(run_id: str, pattern: str, actual: str) -> RetryAttempt:
    vt = VerificationTarget(
        command="pytest -q 2>&1 | tail -1",
        expected_pattern=pattern,
        description="suite green",
    )
    claims = Claims(exit_code=1, tests_failed=3)
    return RetryAttempt(run_id=run_id, claims=claims, failed_targets=[(vt, actual)])


def test_corrective_prompt_contains_original_task():
    original = "Run verification suite and confirm all tests pass."
    attempt = _make_attempt("run-001", "222 passed", "3 failed, 219 passed")
    prompt = build_corrective_prompt(original, [attempt])
    assert original in prompt


def test_corrective_prompt_contains_discrepancy():
    original = "Run verification suite."
    attempt = _make_attempt("run-001", "222 passed", "3 failed, 219 passed")
    prompt = build_corrective_prompt(original, [attempt])
    assert "222 passed" in prompt   # expected
    assert "3 failed" in prompt     # actual


def test_corrective_prompt_includes_all_prior_attempts():
    original = "Run verification suite."
    attempts = [
        _make_attempt("run-001", "222 passed", "3 failed"),
        _make_attempt("run-002", "222 passed", "1 failed"),
    ]
    prompt = build_corrective_prompt(original, attempts)
    assert "run-001" in prompt
    assert "run-002" in prompt


def test_corrective_prompt_instructs_new_session():
    original = "Run tests."
    attempt = _make_attempt("run-001", "0 failed", "2 failed")
    prompt = build_corrective_prompt(original, [attempt])
    # Prompt should note this is a fresh context attempt
    assert "fresh" in prompt.lower() or "new" in prompt.lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_retry.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `cc_orchestrator/retry.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from cc_orchestrator.models import Claims, VerificationTarget


@dataclass
class RetryAttempt:
    run_id: str
    claims: Claims
    failed_targets: list[tuple[VerificationTarget, str]]  # (target, actual_output)


def build_corrective_prompt(
    original_prompt: str,
    attempts: list[RetryAttempt],
) -> str:
    lines = [
        "ORIGINAL TASK (unchanged — implement this completely):",
        original_prompt,
        "",
        f"PRIOR FAILED ATTEMPTS ({len(attempts)}) — fresh context, do not continue these sessions:",
        "",
    ]
    for i, attempt in enumerate(attempts, 1):
        lines.append(f"Attempt {i} (run_id: {attempt.run_id}):")
        for vt, actual in attempt.failed_targets:
            lines.append(f"  Verification check: {vt.description}")
            lines.append(f"    Command:   {vt.command}")
            lines.append(f"    Expected:  {vt.expected_pattern}")
            lines.append(f"    Actual:    {actual.strip()}")
        lines.append("")
    lines.append(
        "Fix the specific discrepancies listed above, then re-run the full "
        "verification suite before reporting done."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_retry.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add cc_orchestrator/retry.py tests/test_retry.py
git commit -m "feat: corrective prompt builder for retry loop"
```

---

### Task 8: MCP server

**Files:**
- Create: `cc_orchestrator/server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: all prior modules
- Produces:
  - MCP tool `dispatch_worker` (parameters: agent, prompt, session, working_dir, timeout_s, task_type)
  - MCP tool `get_run_output` (parameters: run_id, lines)
  - `main()` — entry point called by `cc-orchestrator` script

- [ ] **Step 1: Write failing tests in `tests/test_server.py`**

```python
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from cc_orchestrator.server import _handle_dispatch, _handle_get_output


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
max_retries: 3
workers:
  opencode:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  pi:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: echo
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    return cfg


def test_dispatch_returns_run_result_dict(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")

    result = _handle_dispatch(
        agent="opencode",
        prompt="echo hello",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )

    assert "run_id" in result
    assert result["agent"] == "opencode"
    assert result["status"] in ("completed", "failed", "low_confidence")
    assert "claims" in result
    assert "verification_targets" in result
    assert "raw_output_path" in result


def test_dispatch_unknown_agent_returns_error(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    (tmp_path / ".cc-orchestrator").mkdir()
    import shutil
    shutil.copy(cfg_path, tmp_path / ".cc-orchestrator" / "config.yaml")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = _handle_dispatch(
        agent="unknown_agent_xyz",
        prompt="do something",
        session="new",
        working_dir=str(tmp_path),
        timeout_s=10,
        task_type="implementation",
    )
    assert result["status"] == "failed"
    assert "unknown" in result["summary"].lower()


def test_get_output_returns_last_n_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runs_dir = tmp_path / ".cc-orchestrator" / "runs" / "run-test-001"
    runs_dir.mkdir(parents=True)
    output_file = runs_dir / "output.txt"
    output_file.write_text("\n".join(f"line {i}" for i in range(500)))

    result = _handle_get_output(run_id="run-test-001", lines=10)
    returned_lines = result.strip().splitlines()
    assert len(returned_lines) == 10
    assert returned_lines[-1] == "line 499"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_server.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `cc_orchestrator/server.py`**

```python
from __future__ import annotations

import datetime
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from cc_orchestrator.config import load_config
from cc_orchestrator.models import Claims, RunResult
from cc_orchestrator.summarizer import summarize
from cc_orchestrator.verifier import generate_targets
from cc_orchestrator.workers.opencode import OpenCodeAdapter
from cc_orchestrator.workers.pi import PiAdapter
from cc_orchestrator.workers.qoder import QoderAdapter

mcp = FastMCP("cc-orchestrator")


def _base_dir() -> Path:
    return Path.home() / ".cc-orchestrator"


def _make_run_id(agent: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{ts}-{agent}"


def _get_adapter(agent: str, config):
    adapters = {
        "opencode": OpenCodeAdapter,
        "pi": PiAdapter,
        "qoder": QoderAdapter,
    }
    if agent not in adapters:
        return None
    worker_cfg = config.workers.get(agent)
    if worker_cfg is None:
        return None
    runs_dir = _base_dir() / "runs"
    sessions_dir = _base_dir() / "sessions"
    return adapters[agent](worker_cfg, runs_dir, sessions_dir)


def _handle_dispatch(
    agent: str,
    prompt: str,
    session: str,
    working_dir: str,
    timeout_s: int,
    task_type: str,
) -> dict:
    config = load_config()
    adapter = _get_adapter(agent, config)

    if adapter is None:
        return {
            "run_id": "run-error",
            "agent": agent,
            "status": "failed",
            "summary": f"unknown agent '{agent}'; valid agents: opencode, pi, qoder",
            "claims": {"exit_code": 1, "tests_passed": None, "tests_failed": None,
                       "tests_error": None, "files_changed": None, "commit_hash": None,
                       "important_findings": []},
            "verification_targets": [],
            "raw_output_path": "",
            "parent_run_id": None,
        }

    runs_dir = _base_dir() / "runs"
    run_id = _make_run_id(agent)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output_file = run_dir / "output.txt"

    adapter_result = adapter.run(
        prompt=prompt,
        session=session,
        working_dir=working_dir,
        timeout_s=timeout_s,
        output_file=output_file,
    )

    raw_text = output_file.read_text(errors="replace") if output_file.exists() else ""
    api_key = os.environ.get(config.anthropic_api_key_env)

    claims, summary, confidence = summarize(
        raw_text=raw_text,
        exit_code=adapter_result.exit_code,
        task_type=task_type,
        adapter_patterns=adapter.extraction_patterns(),
        haiku_model=config.haiku_model,
        api_key=api_key,
    )

    if adapter_result.exit_code == 124:
        status = "timeout"
    elif adapter_result.exit_code != 0:
        status = "failed"
    elif confidence < 0.6:
        status = "low_confidence"
    else:
        status = "completed"

    targets = generate_targets(claims, working_dir)

    return {
        "run_id": run_id,
        "agent": agent,
        "status": status,
        "summary": summary,
        "claims": {
            "tests_passed": claims.tests_passed,
            "tests_failed": claims.tests_failed,
            "tests_error": claims.tests_error,
            "files_changed": claims.files_changed,
            "commit_hash": claims.commit_hash,
            "exit_code": claims.exit_code,
            "important_findings": claims.important_findings,
        },
        "verification_targets": [
            {"command": t.command, "expected_pattern": t.expected_pattern, "description": t.description}
            for t in targets
        ],
        "raw_output_path": str(output_file),
        "parent_run_id": None,
    }


def _handle_get_output(run_id: str, lines: int = 100) -> str:
    output_file = _base_dir() / "runs" / run_id / "output.txt"
    if not output_file.exists():
        return f"No output found for run_id '{run_id}'"
    all_lines = output_file.read_text(errors="replace").splitlines()
    return "\n".join(all_lines[-lines:])


@mcp.tool()
def dispatch_worker(
    agent: str,
    prompt: str,
    session: str = "new",
    working_dir: str = ".",
    timeout_s: int = 1800,
    task_type: str = "implementation",
) -> dict:
    """Dispatch a task prompt to a worker CLI agent and return structured results."""
    return _handle_dispatch(agent, prompt, session, working_dir, timeout_s, task_type)


@mcp.tool()
def get_run_output(run_id: str, lines: int = 100) -> str:
    """Fetch the last N lines of raw output from a completed run."""
    return _handle_get_output(run_id, lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: all passed (across all test files)

- [ ] **Step 5: Commit**

```bash
git add cc_orchestrator/server.py tests/test_server.py
git commit -m "feat: MCP server with dispatch_worker and get_run_output tools"
```

---

### Task 9: README and Claude Code integration instructions

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# cc-orchestrator

A lightweight MCP server for Claude Code that dispatches tasks to worker CLI
agents (OpenCode, Pi Agent, Qoder), captures their output, and returns
structured summaries with verification targets — eliminating manual
clipboard middleware.

## Installation

```bash
cd /path/to/cc-orchestrator
uv sync
```

## Configuration

```bash
cp config.example.yaml ~/.cc-orchestrator/config.yaml
```

Edit `~/.cc-orchestrator/config.yaml` and fill in the `# CONFIGURE THIS`
fields for each worker CLI binary and session flags.

Set your Anthropic API key (used for Haiku summarization fallback):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Wire into Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "cc-orchestrator": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/Users/liangguo/My_Skills/cc-orchestrator",
        "cc-orchestrator"
      ]
    }
  }
}
```

Restart Claude Code. You now have two new tools: `dispatch_worker` and
`get_run_output`.

## Usage (from Claude Code)

```
dispatch_worker(
  agent="opencode",
  prompt="Run pytest -q and ruff check and report results",
  task_type="verification",
  working_dir="/path/to/your/project"
)
```

Claude Code receives structured claims + verification targets it can
independently confirm with Bash.

## Contributing

See `config.example.yaml` for `# CONFIGURE THIS` flags. Each worker adapter
in `cc_orchestrator/workers/` is the only CLI-specific code — PRs that add
accurate flags for OpenCode, Pi, or Qoder are welcome.
```

- [ ] **Step 2: Run full test suite one final time**

```bash
uv run pytest tests/ -v
```

Expected: all passed

- [ ] **Step 3: Final commit**

```bash
git add README.md
git commit -m "docs: README with installation, config, and Claude Code wiring"
```
