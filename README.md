# cc-orchestrator

A lightweight MCP server for Claude Code that dispatches tasks to worker CLI
agents (Pi Agent, Qoder), captures their output, and returns structured
summaries with verification targets — eliminating manual clipboard
middleware.

OpenCode was supported in earlier versions but was removed: its headless-mode
CLI invocation repeatedly regressed back to printing top-level `--help`
instead of running the prompt, across multiple fix attempts. Pi and Qoder
have run reliably in headless mode.

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
fields for each worker CLI binary and session flags.

Set your Anthropic API key (used for Haiku summarization fallback):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Wire into Claude Code

Run once to register the MCP server:

```bash
claude mcp add cc-orchestrator -- uv run --project /path/to/cc-orchestrator cc-orchestrator
```

Replace `/path/to/cc-orchestrator` with your actual clone path. Restart Claude Code. You now have two new tools: `dispatch_worker` and `get_run_output`.

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

## Usage (from Claude Code)

```
dispatch_worker(
  agent="pi",
  prompt="Implement story 10.6 per the spec",
  task_type="implementation",
  working_dir="/path/to/your/project"
)
```

Claude Code receives structured claims + verification targets it can
independently confirm with Bash.

### Role convention

The server enforces a default role mapping between `task_type` and `agent`
(pass `override_role=True` on `dispatch_worker` to bypass it for one call):

| `task_type` | `agent` | Purpose |
|---|---|---|
| `implementation` | `pi` | Implements stories/fixes |
| `code_review_heavy` | `qoder` | Thorough, slower review pass |

`code_review_light` and other `task_type` values (`verification`,
`close_out`, `code_review`) are unrestricted — dispatch to `qoder` or `pi`
for a fast/independent second opinion. Because `dispatch_worker` runs each
CLI as an asyncio subprocess, independent calls — e.g. two fresh sessions
reviewing the same diff — execute concurrently rather than queuing behind
each other:

```
dispatch_worker(agent="qoder", task_type="code_review_heavy", prompt="...", working_dir="...")
dispatch_worker(agent="pi", task_type="code_review_light", prompt="...", working_dir="...")
```

Issue both calls in the same turn and they run in parallel.

### Headless CLI invocation — read this before configuring a new worker

Each worker CLI defaults to an interactive TUI; the config must point at its
*non-interactive* mode or the subprocess will never exit:

- **Pi**: requires `-p`/`--print` in `extra_flags`, or it drops into its
  interactive session instead of processing the prompt and exiting.
- **Qoder**: requires `-p`/`--print` in `extra_flags` (not conditional on
  `new_session_flag`, so it still applies on a `continue` dispatch).

`config.example.yaml` has both configured correctly as a reference.

## Known v1 Limitations

- **`session="continue"` always falls back to `"new"`**: Worker session IDs are not yet captured from CLI output. This is a no-op in v1; all dispatches start fresh.
- **Project key is the working directory folder name**: Two projects with the same folder name share a session file. Use distinct folder names for now.

## Contributing

See `config.example.yaml` for `# CONFIGURE THIS` flags. Each worker adapter
in `cc_orchestrator/workers/` is the only CLI-specific code — PRs that add
accurate flags for Pi or Qoder, or a new worker CLI, are welcome.
