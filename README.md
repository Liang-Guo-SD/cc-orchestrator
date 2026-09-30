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
