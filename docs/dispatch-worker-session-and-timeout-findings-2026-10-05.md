# `dispatch_worker` findings — session resumption & timeout visibility (2026-10-05)

Found during a real `pi` implementation dispatch (Feishubot_V4, Story 11.6) that ran
long and timed out twice. Not a crash or data-loss bug — the underlying worker kept
making real progress — but two gaps made the MCP interface unreliable as the sole
way to tell what was happening.

## 1. `session` is effectively a no-op beyond "new" — already known, but undocumented at the MCP layer

Confirmed in code (`cc_orchestrator/workers/cli_adapter.py`): `save_session` is
imported but never called, and `AdapterResult.session_id` is hardcoded to `None`
(`server.py` never calls `save_session` either). So `load_session` always returns
`None`, and `session="continue"` always silently falls back to `"new"` — exactly as
`README.md`'s "Known v1 Limitations" already says.

**What's missing:** that limitation lives only in `README.md`. The `dispatch_worker`
tool's own docstring (what an MCP caller actually sees) says nothing about it, and
the `session` parameter's schema has no description at all. A caller has no way to
discover this without reading the repo source.

**Compounding gap for `pi` specifically:** `pi`'s `new_session_flag: ""` is empty in
`config.example.yaml`, so even `session="new"` adds no CLI flag — every dispatch to
`pi`, regardless of what `session` value is passed, produces the identical
invocation (`pi --approve --print "<prompt>"`). There is currently no way to force,
resume, or target a specific `pi` session through this tool at all.

**What actually happened in practice:** I passed an arbitrary session UUID (scraped
from `~/.pi/agent/sessions/.../*.jsonl`, an undocumented path internal to the `pi`
CLI, not exposed by cc-orchestrator) as `session=`, expecting resumption. It fell
through both the `"continue"` and `"new"` branches (matches neither string) and ran
with no session flags — i.e. a brand-new `pi` process. It answered a yes/no question
about file existence correctly, which looked like successful resumption but wasn't:
a fresh session with filesystem access would get that answer right regardless.
**Passing an unrecognized `session` value is accepted silently, with no error or
warning, and produces the same no-op invocation as every other value.**

**Suggested fix directions:**
- Surface the "continue is a no-op in v1" limitation directly in the `dispatch_worker` docstring/schema, not just the README.
- Either validate `session` against `{"new", "continue"}` and reject other values with a clear error, or implement real session-id capture/resume (`save_session` is already written and imported — it's just never invoked).
- Give `pi` a real `new_session_flag` in `config.example.yaml` if the underlying CLI has one, so `session="new"` is not silently identical to every other value.

## 2. Timeout gives zero partial-progress visibility, and `raw_output_path` is not a reliable progress signal for `pi`

Two consecutive dispatches to `pi` (1800s, then 3000s) both returned
`status: "timeout"`, `exit_code: 124`, every `claims` field `null`, and an
`output.txt` containing only a Node.js startup warning (2 lines) — despite `pi`
having made substantial, verifiable progress in both runs (confirmed via `git diff
--stat` in the working directory, and by reading `pi`'s own private transcript at
`~/.pi/agent/sessions/<project>/<timestamp>_<uuid>.jsonl`, which is ~100 real
tool-call entries per run, actively growing until shortly before the timeout fired).

So the MCP result and `get_run_output` (which just tails the same near-empty
`output.txt`) gave no indication that the worker was alive, making real edits, or
how far it had gotten — the caller has to go outside the tool entirely (shell into
the working directory and a `pi`-specific, undocumented log path) to tell a hung
worker from a slow-but-working one. This is a real operational problem: the natural
reaction to a `status: "timeout"` with empty output and a timed-out `ps` CPU
reading is to assume it's stuck and kill it, which would have discarded real,
in-progress work here.

Likely root cause: `pi --print` (headless mode) doesn't stream its tool-call
transcript to stdout — that only goes to its own session log — so
`cli_adapter.py`'s stdout-capture-only approach (`async for chunk in proc.stdout`)
has nothing meaningful to capture for this worker, timeout or not.

**Suggested fix directions:**
- On timeout, before returning, run `git diff --stat` (and maybe `git status
  --porcelain`) in `working_dir` and include it in the result — cheap, always
  available, and immediately answers "did anything real happen."
- Investigate whether `pi --print` has a verbosity/streaming flag that would put
  real tool-call activity into stdout; if not, consider teaching the `PiAdapter`
  where `pi` keeps its own session transcript (`~/.pi/agent/sessions/`) and surface
  the last few entries from *that* instead of relying solely on stdout.
- At minimum, note in the docstring that `raw_output_path` may be near-empty for
  workers that keep their real transcript elsewhere, so a timeout's empty output
  isn't mistaken for "nothing happened."
