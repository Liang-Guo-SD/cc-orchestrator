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
  agent="opencode",
  prompt="Run pytest -q and ruff check and report results",
  task_type="verification",
  working_dir="/path/to/your/project"
)
```

Claude Code receives structured claims + verification targets it can
independently confirm with Bash.

## Known v1 Limitations

- **`session="continue"` always falls back to `"new"`**: Worker session IDs are not yet captured from CLI output. This is a no-op in v1; all dispatches start fresh.
- **Project key is the working directory folder name**: Two projects with the same folder name share a session file. Use distinct folder names for now.

## Contributing

See `config.example.yaml` for `# CONFIGURE THIS` flags. Each worker adapter
in `cc_orchestrator/workers/` is the only CLI-specific code — PRs that add
accurate flags for OpenCode, Pi, or Qoder are welcome.
