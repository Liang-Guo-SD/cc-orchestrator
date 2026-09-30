# cc-orchestrator Design Spec

**Date:** 2026-09-30  
**Status:** Approved — ready for implementation planning  
**Author:** Liang Guo  
**Location:** `/Users/liangguo/My_Skills/cc-orchestrator/`

---

## 1. Problem Statement

A Lead Software Engineer uses Claude Code as a Commander/Architect agent while
dispatching implementation work to three worker CLI agents: OpenCode, Pi Agent,
and Qoder. Today the coordination is manual:

1. Claude Code writes a task prompt.
2. The engineer copies it into a worker terminal.
3. The engineer waits, copies the output back, pastes it into Claude Code.

This creates three compounding problems:

- **Clipboard overhead:** High cognitive load and wasted time on every step.
- **Context contamination:** Raw terminal output (stack traces, full diffs,
  verbose logs) floods Claude Code's context window, degrading planning quality
  over long sessions.
- **No independent verification:** Claude Code has no way to check worker
  claims; it must trust whatever the engineer pastes.

**Goal:** A lightweight MCP server that eliminates the clipboard and gives
Claude Code independent verification — without changing the worker CLIs.

---

## 2. Architecture

Three components with distinct responsibilities:

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code (Commander)                                 │
│                                                          │
│  1. Calls dispatch_worker(agent, prompt)                 │
│  4. Receives {summary, claims, verification_targets}     │
│  5. Runs verification_targets independently via Bash     │
│  6. Auto-retries (≤ max_retries) if claims ≠ reality     │
└────────────────────┬────────────────────────────────────┘
                     │ MCP protocol (JSON-RPC over stdio)
┌────────────────────▼────────────────────────────────────┐
│  cc-orchestrator MCP Server                              │
│                                                          │
│  - Spawns worker CLI as a subprocess                     │
│  - Captures all stdout/stderr to a local run file        │
│  - Extracts artifacts via rule-based patterns            │
│  - Falls back to Haiku summarization when needed         │
│  - Returns structured JSON to Claude Code                │
└────────────────────┬────────────────────────────────────┘
                     │ subprocess
┌────────────────────▼────────────────────────────────────┐
│  Worker CLIs (unchanged)                                 │
│  OpenCode · Pi Agent · Qoder                             │
│  Each receives a self-contained prompt, runs, exits      │
└─────────────────────────────────────────────────────────┘
```

**Key invariant:** The MCP server never verifies anything itself. It reports
what the worker produced. Claude Code is the only verifier, running independent
Bash commands with no MCP server involvement.

---

## 3. MCP Tool API

### 3.1 `dispatch_worker`

Dispatches a task prompt to a worker CLI and returns a structured result.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent` | `"opencode" \| "pi" \| "qoder"` | Yes | — | Target worker |
| `prompt` | `str` | Yes | — | Full self-contained task prompt |
| `session` | `"new" \| "continue"` | No | `"new"` | `new` = fresh context; `continue` = resume last session for this agent+project |
| `working_dir` | `str` | No | cwd | Absolute path to the project repository |
| `timeout_s` | `int` | No | `1800` | Max seconds to wait before returning `"timeout"` |
| `task_type` | `"implementation" \| "verification" \| "code_review" \| "close_out"` | No | `"implementation"` | Tells the summarizer which fields to expect; determines Haiku invocation threshold |

**Return schema:**

```json
{
  "run_id":    "run-20260930-143022-opencode",
  "agent":     "opencode",
  "status":    "completed | failed | timeout | low_confidence",
  "summary":   "One-sentence prose summary of what was accomplished.",
  "claims": {
    "tests_passed":   222,
    "tests_failed":   0,
    "tests_error":    0,
    "files_changed":  ["app/workflow/r3.py", "tests/test_r3_approval.py"],
    "commit_hash":    "22bdd4d",
    "exit_code":      0
  },
  "verification_targets": [
    {
      "command":          ".venv/bin/python -m pytest -q 2>&1 | tail -1",
      "expected_pattern": "222 passed",
      "description":      "pytest suite fully green"
    },
    {
      "command":          "git show --stat 22bdd4d",
      "expected_pattern": "8 files changed",
      "description":      "commit contains exactly 8 files"
    }
  ],
  "raw_output_path": "~/.cc-orchestrator/runs/run-20260930-143022-opencode/output.txt",
  "parent_run_id":   null
}
```

**Status values:**

| Value | Meaning |
|---|---|
| `"completed"` | Worker exited 0; rule-based confidence ≥ 0.6 |
| `"failed"` | Worker exited non-zero |
| `"timeout"` | Worker exceeded `timeout_s` |
| `"low_confidence"` | Worker exited 0 but neither rule-based nor Haiku could populate ≥ 60% of expected fields |

All `claims` fields default to `null` when not extractable. An empty
`verification_targets` array means the server found nothing checkable; Claude
Code should fall back to `get_run_output` and read the raw tail.

---

### 3.2 `get_run_output`

Fetches the raw tail of a completed run's captured output.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `run_id` | `str` | — | From a prior `dispatch_worker` result |
| `lines` | `int` | `100` | Last N lines of the output file |

**Returns:** Plain text string (the raw tail). Claude Code calls this when a
verification target fails and it needs to see the actual error before writing
a corrective retry prompt.

---

## 4. Worker Adapters

Each worker has its own adapter module in `workers/`. All three implement the
same internal interface so the MCP server is CLI-agnostic.

### 4.1 Common interface

```python
class WorkerAdapter:
    def run(
        self,
        prompt: str,
        session: str,          # "new" | "continue"
        working_dir: str,
        timeout_s: int,
        output_file: Path,     # MCP server provides; adapter writes all output here
    ) -> AdapterResult:
        ...

@dataclass
class AdapterResult:
    exit_code: int
    session_id: str | None     # session ID produced, for future "continue" calls
```

Each adapter is also responsible for declaring the **extraction patterns** it
knows how to apply to its CLI's output. These patterns are what become
`verification_targets`. If an adapter has no pattern for a field, it omits it
rather than guessing.

### 4.2 Session management

Session IDs are persisted in
`~/.cc-orchestrator/sessions/<project_fingerprint>/<agent>.json`.

- `session="new"`: adapter ignores any stored session ID; starts a fresh context.
- `session="continue"`: adapter reads the stored session ID and passes it to
  the CLI's native session flag.

Each adapter owns the translation between `"new"/"continue"` and its CLI's
actual flags. This is the **only place** in the codebase that needs updating
when a CLI changes its session interface.

### 4.3 CLI configuration

Exact binary paths and session flags are not hardcoded. They are read from
`~/.cc-orchestrator/config.yaml` (see Section 7). Adapters have clearly marked
`# CONFIGURE THIS` comments at every CLI-specific flag for open-source
contributors to fill in.

---

## 5. Summarization Pipeline

Two stages run in sequence. Haiku is invoked only when rule-based confidence
falls below 0.6.

### 5.1 Stage 1 — Rule-based extractor

Scans the raw output file for known patterns:

| Field | Pattern |
|---|---|
| `tests_passed` | `(\d+) passed` |
| `tests_failed` | `(\d+) failed` |
| `tests_error` | `(\d+) error` |
| `ruff_clean` | `All checks passed` |
| `ruff_errors` | `Found (\d+) error` |
| `wiring_score` | `(\d+)/(\d+) passed` |
| `commit_hash` | `\[[\w/-]+ ([0-9a-f]{7,})\]` |
| `files_changed` | `(\d+) files? changed` |
| `exit_code` | captured directly from subprocess |

**Confidence score:** fraction of task-type-expected fields that were populated.
Expected fields per task type:

| Task type | Expected fields |
|---|---|
| `"verification"` | tests_passed, tests_failed, ruff_clean, wiring_score |
| `"close_out"` | tests_passed, tests_failed, commit_hash, files_changed |
| `"code_review"` | (no structured fields; confidence always 0 → Haiku always used) |
| `"implementation"` | tests_passed, tests_failed |

### 5.2 Stage 2 — Haiku fallback

Invoked when confidence < 0.6 **or** when `task_type="code_review"`.

Input: last 200 lines of raw output (cost control).

Prompt:

> "This is the tail of a software engineering task's terminal output. Extract
> as JSON: `summary` (one sentence), `tests_passed` (int or null),
> `tests_failed` (int or null), `files_changed` (list of strings or null),
> `commit_hash` (string or null), `important_findings` (list of strings,
> max 3)."

Haiku fills any null fields left by rule-based extraction. The prose `summary`
field always comes from Haiku; rule-based extraction never writes prose.

### 5.3 Final combination

```
claims        = rule_based results  (authoritative for structured fields)
claims       += haiku fills         (for any remaining null fields)
summary       = haiku prose         (always)
confidence    = fraction populated after both stages
```

If confidence remains < 0.6 after both stages, `status` is set to
`"low_confidence"` and `verification_targets` is left empty.

---

## 6. Verification Protocol

### 6.1 `verification_targets` generation

For every non-null field in `claims`, the server generates a corresponding
shell command for Claude Code to run independently. Pattern:

| Claim field | Generated command | Expected pattern |
|---|---|---|
| `tests_passed: N` | `pytest -q 2>&1 \| tail -1` | `"N passed"` |
| `ruff_clean: true` | `uvx ruff@latest check app tests scripts` | `"All checks passed"` |
| `commit_hash: "abc1234"` | `git show --stat abc1234` | `"files changed"` |
| `files_changed: [N files]` | same as above | `"N files changed"` |

`expected_pattern` is always a plain substring match — no regex.

### 6.2 Independence invariant

Claude Code runs each `verification_target.command` via its own **Bash tool**
directly. The MCP server has no involvement after returning the result. The
server cannot intercept or influence what Claude Code observes.

### 6.3 Pass / fail logic

A target **passes** if `expected_pattern` appears anywhere in stdout+stderr.  
A target **fails** if the pattern is absent or the command exits non-zero with
no pattern match.  
**All targets must pass** for the task to be confirmed. One failure triggers
the retry loop.

---

## 7. Retry Loop

The retry loop lives entirely in Claude Code. The MCP server is not involved
after returning its result.

### 7.1 Triggers

- Any `verification_target` fails
- Worker `status` is `"failed"` or `"timeout"`
- Worker `status` is `"low_confidence"`

### 7.2 Corrective prompt construction

Claude Code builds a new prompt containing:
1. The original task prompt (unchanged)
2. What the worker claimed vs what independent verification found
3. A targeted instruction to fix the specific discrepancy

Retries always use `session="new"` — never continue a session that produced
incorrect output. The corrective prompt is the full context; Git is the memory.

### 7.3 Retry limit and escalation

| Attempt | Behaviour |
|---|---|
| 1st failure | Corrective prompt, dispatch again (`session="new"`) |
| 2nd failure | Corrective prompt including both prior failure descriptions |
| 3rd failure | Corrective prompt including all prior failure descriptions |
| 4th failure | Stop. Return `status="retry_limit_reached"`. Surface to user. |

Default limit: 3. Configurable via `max_retries` in `~/.cc-orchestrator/config.yaml`.

### 7.4 Run history

Each retry records a `parent_run_id` linking it to the original dispatch.
All raw output files are preserved at `~/.cc-orchestrator/runs/`. The user
can inspect what each attempt produced without re-running anything.

---

## 8. Configuration

`~/.cc-orchestrator/config.yaml` (machine-local, not committed to the repo):

```yaml
max_retries: 3

workers:
  opencode:
    bin: opencode              # binary name or absolute path
    new_session_flag: ""       # CLI flag to start a fresh session  # CONFIGURE THIS
    continue_flag: ""          # CLI flag to resume a session        # CONFIGURE THIS
    session_id_flag: ""        # CLI flag to pass session ID         # CONFIGURE THIS
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
anthropic_api_key_env: "ANTHROPIC_API_KEY"   # env var name that holds the key
```

A `config.example.yaml` ships with the repository. Users copy it to
`~/.cc-orchestrator/config.yaml` and fill in their CLI flags.

---

## 9. Project Structure

```
cc-orchestrator/
  server.py                    MCP server entry point (JSON-RPC over stdio)
  config.py                    Loads and validates ~/.cc-orchestrator/config.yaml
  summarizer.py                Rule-based extractor + Haiku fallback
  verifier.py                  verification_targets generator from claims
  retry.py                     Corrective prompt builder; retry state tracker
  workers/
    base.py                    AdapterResult dataclass + WorkerAdapter ABC
    opencode.py                OpenCode CLI adapter
    pi.py                      Pi Agent CLI adapter
    qoder.py                   Qoder CLI adapter
  docs/
    superpowers/specs/
      2026-09-30-cc-orchestrator-design.md   (this file)
  config.example.yaml          Template for ~/.cc-orchestrator/config.yaml
  pyproject.toml               Python package metadata + dependencies
  README.md                    Setup, configuration, usage, contributing
```

---

## 10. Dependencies

| Package | Purpose |
|---|---|
| `mcp` | MCP server SDK (Anthropic) |
| `anthropic` | Haiku API calls for summarization fallback |
| `pyyaml` | Config file parsing |
| `pytest` (dev) | Tests for summarizer and verifier logic |

No GUI dependencies. No persistent background daemon. The server is spawned
by Claude Code on demand via the MCP stdio transport and exits when the
Claude Code session ends.

---

## 11. Out of Scope

The following are explicitly deferred to future versions or community
contributions:

- A CLI wrapper (`cc` command) around the MCP server for use outside Claude Code
- Support for additional worker CLIs beyond OpenCode, Pi, Qoder
- A web dashboard or GUI for run history
- Streaming partial output back to Claude Code before the worker finishes
- Per-project config overrides (only `~/.cc-orchestrator/config.yaml` is supported in v1)
- Windows support (macOS and Linux only for v1)
