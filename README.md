# cc-orchestrator

A lightweight MCP server for Claude Code that dispatches tasks to the Pi Agent CLI, watches its
real progress while it runs, and returns structured summaries with verification targets —
eliminating manual clipboard middleware and silent-timeout guessing games.

OpenCode and Qoder were supported in earlier versions and were removed: OpenCode's headless-mode
CLI invocation repeatedly regressed back to printing top-level `--help` instead of running the
prompt, across multiple fix attempts; Qoder was dropped to simplify the codebase to the worker
actually in active use. Pi has run reliably in headless mode. The adapter interface in
`cc_orchestrator/workers/` is written so adding a worker back is a new adapter class, not an
interface change.

## Installation

```bash
cd /path/to/cc-orchestrator
uv sync
```

**Note:** This project requires MCP v2+ (the MCP package has been updated;
`MCPServer` replaces `FastMCP`, but this is handled transparently by the server code).

## Configuration

```bash
cp config.example.yaml ~/.cc-orchestrator/config.yaml
```

Edit `~/.cc-orchestrator/config.yaml` and fill in the `# CONFIGURE THIS`
fields for the `pi` binary and session flags. `watch_interval_s`,
`idle_threshold_s`, and `error_loop_threshold` tune the background watcher
(see "How dispatch_worker works" below) and have sane defaults if omitted.

Set your Anthropic API key (used for Haiku summarization fallback):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Wire into Claude Code

Run once to register the MCP server:

```bash
claude mcp add cc-orchestrator -- uv run --project /path/to/cc-orchestrator cc-orchestrator
```

Replace `/path/to/cc-orchestrator` with your actual clone path. Restart Claude Code. You now have
four new tools: `dispatch_worker`, `get_run_status`, `get_run_progress`, `get_run_output`.

Alternatively, add a project-level `.mcp.json` at the root of your repo:

```json
{
  "mcpServers": {
    "cc-orchestrator": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/cc-orchestrator",
        "cc-orchestrator"
      ]
    }
  }
}
```

## How dispatch_worker works

`dispatch_worker` returns **immediately** with `{run_id, status: "running"}` — it does not block
until the worker finishes. A background watcher task starts alongside the worker and tails pi's
own session transcript (`~/.pi/agent/sessions/...`) plus `git status`/`git diff --stat` in
`working_dir`, so you can poll for progress instead of waiting out the full `timeout_s` to find
out whether anything happened:

```
dispatch_worker(prompt="Implement story 10.6 per the spec", task_type="implementation",
                working_dir="/path/to/your/project")
# -> {"run_id": "run-20261005-143000-pi-a1b2c3", "status": "running"}

get_run_status(run_id="run-20261005-143000-pi-a1b2c3")
# -> {"status": "running", "alert": null, "idle_seconds": 12, "files_touched": ["app/foo.py"], ...}
# or, once finished:
# -> {"status": "completed", "claims": {...}, "verification_targets": [...], ...}
```

`alert` is non-null when the watcher's trip-wire has fired — either a repeated same-tool failure
(pi stuck retrying the same thing without making progress) or an idle stall (no transcript
activity for `idle_threshold_s`, process still running). It's a cheap pattern match meant to
prompt you to look closer, not a verdict — check `get_run_output`/the working directory yourself
before deciding to kill a run. `get_run_progress` does everything `get_run_status` does, plus
spends a small Haiku call to turn the raw facts into a one-line human-readable status; poll it
less frequently since it isn't free.

### Parallel dispatch

Since each `dispatch_worker` call spawns an independent `asyncio` subprocess and returns
immediately, issue several calls in the same turn for independent sub-tasks and they run
concurrently — this is the intended pattern for unrelated work, not something that needs
sequential dispatch-and-wait:

```
dispatch_worker(prompt="Implement story 10.6", task_type="implementation", working_dir="...")
dispatch_worker(prompt="Implement story 10.7", task_type="implementation", working_dir="...")
```

Then poll each returned `run_id` independently with `get_run_status`.

### Headless CLI invocation

Pi defaults to an interactive TUI; the config must point at its non-interactive mode or the
subprocess will never exit: `pi` requires `-p`/`--print` in `extra_flags`, or it drops into its
interactive session instead of processing the prompt and exiting. `config.example.yaml` has this
configured correctly as a reference.

## Known Limitations

- **`session="continue"` always falls back to `"new"`**: Worker session IDs are not yet captured
  from CLI output. `session` values outside `{"new", "continue"}` are rejected with an error.
- **Project key is the working directory folder name**: Two projects with the same folder name
  share a session file (`cc_orchestrator/session_store.py`). This is unrelated to how the watcher
  locates pi's own transcript, which uses the full absolute path, mangled per pi's own
  directory-naming scheme (`cc_orchestrator/workers/pi.py::transcript_path`) — don't conflate the
  two if you're extending either.
- **`raw_output_path`/`get_run_output` may be near-empty for pi**: `pi --print` doesn't stream its
  real tool-call activity to stdout; use `get_run_status`/`get_run_progress` instead of treating
  empty output as evidence that nothing happened.
- **Transcript discovery is a heuristic, not a guarantee**: if two `dispatch_worker` calls target
  the same `working_dir` within a few seconds of each other, the watcher may attach to the wrong
  session transcript (it picks the earliest transcript file modified after the dispatch's own
  start time, minus a small clock-skew buffer).

## Contributing

See `config.example.yaml` for `# CONFIGURE THIS` flags. Each worker adapter
in `cc_orchestrator/workers/` is the only CLI-specific code — PRs that add
accurate flags for Pi, or a new worker CLI (including its own
`transcript_path()` if it keeps a session log), are welcome.
