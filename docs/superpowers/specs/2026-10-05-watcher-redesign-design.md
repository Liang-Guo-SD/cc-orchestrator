# Design: drop qoder, non-blocking dispatch, watcher-based progress/alerting (2026-10-05)

**Status:** approved design, not yet implemented. Supersedes the "incremental path" sketch in
`docs/proposal-watcher-agent-progress-visibility-2026-10-05.md` with concrete decisions; builds
directly on gaps recorded in `docs/dispatch-worker-session-and-timeout-findings-2026-10-05.md`.
Read both first for the incident context that motivated this.

## Problem statement

Two independent problems, both rooted in the same architectural gap:

1. **Operator asked to drop qoder.** Pi has run reliably in headless mode; qoder adds
   maintenance surface (adapter, config, role-convention branches) without being in active use.
   Only Pi should remain as a worker, but `dispatch_worker` should support firing off multiple
   independent Pi dispatches in parallel for unrelated sub-tasks.
2. **Pi can find and even partially fix errors mid-run, but cc-orchestrator never finds out
   until the full `timeout_s` elapses, at which point the result looks identical to a genuine
   hang** (`status: "timeout"`, empty `claims`, near-empty `output.txt` since `pi --print`
   doesn't stream its transcript to stdout). This has already caused a real near-miss: 50
   minutes of real, salvageable progress (confirmed via Pi's own session transcript and `git
   diff --stat`) that looked, from the MCP result alone, indistinguishable from "nothing
   happened, kill it and redispatch."

Root cause of (2): `dispatch_worker`'s current shape is one coroutine, one `await`, one result
(`cli_adapter.py::run`, `asyncio.wait_for(_drain_and_wait(), timeout=timeout_s)`). Nothing
observes the worker between dispatch and the single terminal event (exit or timeout-kill).
Pi is a closed-box headless CLI — we cannot make *it* cooperatively report errors over a
protocol it doesn't implement (ruling out an A2A-style rework, which assumes the remote agent
natively supports a task-lifecycle protocol). The only lever available is for cc-orchestrator
itself to observe Pi's own session transcript (`~/.pi/agent/sessions/<mangled-path>/*.jsonl`)
and git state while the subprocess runs, and surface trip-wire signals proactively.

## Decisions

### 1. Drop qoder

- Delete `cc_orchestrator/workers/qoder.py` and its `config.example.yaml` block.
- Delete `ROLE_ASSIGNMENTS`, `_role_mismatch`, and the `override_role` parameter from
  `server.py` — with one worker, there is nothing left to enforce a convention between.
- Keep the `agent` parameter on `dispatch_worker` (default `"pi"`) for forward compatibility;
  adding a future second worker should mean registering a new adapter, not changing the
  tool's interface.
- `task_type` stays — it still drives `summarizer.py`'s confidence scoring (`_EXPECTED_FIELDS`)
  independent of which agent ran.

### 2. Non-blocking dispatch

`dispatch_worker(...)`:
- Spawns the Pi subprocess (as today, via `asyncio.create_subprocess_exec`).
- Writes `run_dir/meta.json`: `{agent, task_type, prompt, working_dir, pid, started_at, timeout_s}`.
- Starts a background `asyncio.Task` (the watcher, §4) running concurrently on the same event
  loop — not a separate OS process; per the original proposal doc, a task sharing the loop is
  sufficient and keeps this in-process.
- Returns immediately: `{"run_id": ..., "status": "running"}`.

The real `timeout_s` ceiling still applies — it now governs how long the watcher task's own
subprocess-wait lives, not how long the MCP tool call blocks. When the subprocess actually
exits (success, non-zero, or timeout-kill), the watcher runs the same summarization pipeline
(`summarize()` in `summarizer.py`, `generate_targets()` in `verifier.py`) that
`_handle_dispatch` runs today, and writes the result to `run_dir/result.json`.

### 3. New tools, unified response shape

- `get_run_status(run_id)` — no LLM call. Reads `result.json` if present (run finished);
  otherwise reads `progress.json` (run still going) and returns it directly.
- `get_run_progress(run_id)` — same as `get_run_status`, but if the run is still going, also
  runs a Haiku summarization pass over the transcript tail collected so far and includes a
  human-readable one-line status. Meant to be polled less frequently than `get_run_status`
  (costs a small API call); if the run has already finished, behaves identically to
  `get_run_status` (the final result already has a summary).
- `get_run_output(run_id, lines=100)` — unchanged, tails `output.txt`.

Both new tools, and the terminal `result.json`, share one response shape:

```json
{
  "run_id": "...",
  "agent": "pi",
  "status": "running" | "completed" | "failed" | "timeout" | "low_confidence",
  "summary": "...",
  "alert": null | "repeated failure: bash failing repeatedly, latest: ...",
  "idle_seconds": 12,
  "files_touched": ["app/foo.py", "..."],
  "claims": { ... } | null,
  "verification_targets": [...] | [],
  "raw_output_path": "...",
  "parent_run_id": null
}
```

`claims`/`verification_targets` are `null`/`[]` while `status == "running"` (they're only
meaningful once the run is scored), and populated exactly as `dispatch_worker` returns them
today once `status` settles to a terminal value. A caller polling in a loop checks `status`
and doesn't need a separate code path for "mid-run" vs. "final" — this directly answers the
open question in the original proposal doc about designing both as the same type from the
start.

### 4. Watcher mechanics

One `asyncio.Task` per dispatched run, polling every `watch_interval_s` (default 60, from
config). Each tick:

1. **Transcript discovery/incremental read.** `WorkerAdapter` gains an optional method:
   `transcript_path(working_dir: str, started_after: datetime) -> Path | None`. `PiAdapter`
   implements it by replicating Pi's own directory-naming scheme — confirmed by inspection of
   real session files at `~/.pi/agent/sessions/`: the absolute working directory path with
   `/` replaced by `-` and wrapped in `--...--` (e.g. `/Users/liangguo/Feishubot_V4` →
   `--Users-liangguo-Feishubot_V4--`), then the newest `.jsonl` file in that directory created
   at or after the dispatch's `started_at`. This is deliberately **not** the same as
   `session_store.py`'s `project_key` (`Path(working_dir).name`), which is our own, unrelated
   key for the no-op session-continue feature — conflating the two would be a bug. Adapters
   without a discoverable transcript return `None`; the watcher then runs on git-state
   observation alone (§4.5) rather than erroring.
2. The watcher keeps a byte offset into the transcript file and only parses newly appended
   complete JSONL lines each tick (not a full re-read).
3. **Idle tracking:** record the timestamp of the latest parsed entry. `idle_seconds =
   now - last_entry_time` (or `now - started_at` if no entries yet).
4. **Error-loop detection:** maintain a rolling window of the last 5 `message` entries where
   `message.role == "toolResult"`. (Confirmed real shape via inspection: `{"type": "message",
   "message": {"role": "toolResult", "toolCallId": ..., "toolName": ..., "content": [...],
   "isError": true|false}}`.) If the last `error_loop_threshold` (default 3) such entries all
   have `isError: true` **and** share the same `toolName` **and** their `content[0].text`
   values share a long-enough common prefix/substring (simple string containment check — not
   an LLM judgment), set `alert = f"repeated failure: {toolName} failing repeatedly, latest:
   {text[:200]}"`.
5. **Stall detection:** if `idle_seconds > idle_threshold_s` (default 300) **and** the
   subprocess PID is still alive, set `alert = f"no transcript activity for {idle_seconds}s —
   process alive but idle"` (only if no error-loop alert already fired this tick — error-loop
   is the more specific, more actionable signal).
6. **Git fallback/corroboration**, every tick regardless of transcript availability: run `git
   status --porcelain` and `git diff --stat` in `working_dir`; record touched-file count/names.
   This is both the sole signal when no transcript is discoverable, and a sanity check
   ("transcript says idle, but files are still changing" would itself be worth surfacing later
   — not required for this pass, just noted as the reason both signals are kept).
7. Write the current structured snapshot to `run_dir/progress.json` (overwritten each tick).

`alert` is a cheap pattern-matched trip-wire, not a verdict — its only job is telling Claude
Code "look closer here" early, well before `timeout_s` would otherwise be the first signal.

### 5. Explicitly out of scope

- **Session-id capture/resume** (`session="continue"` currently always falls back to `"new"`
  silently — `save_session` is written but never called). Left as-is for this pass; tracked
  separately. The non-blocking redesign slightly raises the stakes of this limitation (parallel
  dispatches to the same `working_dir` would collide on the session-continue feature if it were
  implemented naively, since `session_store.py` keys only by folder basename) — noted in the
  README, not fixed here.
- **Push notifications** (proposal doc's optional step 5) — no host-side notification channel
  has been investigated; not built speculatively.
- Any change to Pi's own CLI invocation/flags beyond what's already in `config.example.yaml`.

### 6. Small included fixes (cheap, already-documented gaps)

From the findings doc, folded into this pass since the same code is being touched anyway:
- `dispatch_worker`'s `session` parameter gets a real docstring and schema description stating
  `"continue"` is currently a no-op fallback to `"new"`.
- `session` values outside `{"new", "continue"}` are now rejected with a clear error instead of
  silently producing the same invocation as every other value.
- `raw_output_path`'s docstring notes it may be near-empty for Pi in headless mode, directing
  callers to `get_run_status`/`get_run_progress` instead of assuming empty output means nothing
  happened.

## Data flow / file layout

```
~/.cc-orchestrator/runs/run-<ts>-pi/
  meta.json       # agent, task_type, prompt, working_dir, pid, started_at, timeout_s
  output.txt      # raw stdout/stderr tail (thin for pi; kept for parity/debugging)
  progress.json   # watcher's live snapshot; overwritten each tick while status == "running"
  result.json     # written once, when the subprocess actually exits or timeout_s fires
```

## Testing strategy

- Unit tests for the error-loop and stall detectors against synthetic JSONL fixtures mirroring
  the real structure confirmed by inspection (clean run, repeated-`isError` run, idle run).
- Unit test for `PiAdapter.transcript_path()` against the real naming scheme.
- Test that `dispatch_worker` returns in well under a second against a fake slow subprocess,
  and that `get_run_status` transitions `"running"` → a terminal status as the fake process
  exits.
- Existing adapter/summarizer/verifier tests updated for qoder removal (delete qoder-specific
  tests, adjust role-assignment tests since that logic is gone).

## Documentation

- README: remove all qoder references; make parallel-Pi-dispatch (not review-pairing) the
  headline usage example; document the new tool set (`dispatch_worker`, `get_run_status`,
  `get_run_progress`, `get_run_output`) and the watcher/`alert` field; update "Known v1
  Limitations" (qoder gone; session-continue still a no-op; note the project-key vs.
  transcript-path-mangling distinction so a future contributor doesn't conflate them).
