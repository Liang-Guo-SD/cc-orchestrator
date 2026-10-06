# Proposal: a watcher agent alongside each dispatched worker (2026-10-05)

**Status:** idea captured for later revision, not implemented. Raised by the project lead
during a real Feishubot_V4 dispatch that illustrates the problem this would fix. Builds
directly on the gaps already recorded in
`dispatch-worker-session-and-timeout-findings-2026-10-05.md` — read that file first; this
proposal is the structural fix for its §2.

## The concrete incident that prompted this

Story 11.7 (Feishubot_V4, fixture loading into drill runs) was dispatched to `pi` with
`timeout_s=3000`. The call returned `status: "timeout"`, `exit_code: 124`, every `claims`
field `null`, and an `output.txt` with nothing useful (`pi --print` doesn't stream its
transcript to stdout — see the existing findings doc). From the coordinator's side this
looked exactly like "nothing happened, maybe it hung."

It had not hung. `git status`/`git diff --stat` in the working directory, plus `pi`'s own
private session transcript at `~/.pi/agent/sessions/<project>/<ts>_<uuid>.jsonl`, showed
~95 real transcript entries: `pi` had written a new 800-line module, found and fixed a real
bug in it (a `dict_row`-vs-tuple assumption that would have broken against the actual
connection pool), and was mid-edit on a cleanup pass when the timeout killed it. Real,
salvageable, in-progress work — but the coordinator had to leave the MCP tool's result
entirely and go spelunking through an undocumented CLI-internal path to find that out, and
only thought to do so because a previous incident had already taught it not to trust an
empty-output timeout. A coordinator without that scar tissue would reasonably have assumed
the dispatch was a dead end and discarded or blindly re-dispatched over it.

**The actual failure isn't that the task took 50 minutes.** Long-running agentic work is
normal and expected. The failure is that the *only* signal available for the entire 50
minutes, and even at the end, was silence — one opaque blocking call that resolves to
either "done" or "killed," with no view into which of "almost done," "stuck in a loop,"
"burning tokens on something out of scope," or "making steady real progress" was actually
happening.

## The proposal

**Dispatch two things, not one: the worker, and a watcher.** The watcher's only job is to
observe the worker's real progress by whatever channel actually carries it (not stdout,
which is empty for these CLIs in headless mode) and surface periodic, human-readable
progress reports back to the coordinator — proactively, not only on request.

Sketch of the division of labor:

- **The worker** is today's `dispatch_worker` call, unchanged in spirit: a CLI subprocess
  given a prompt, working directory, and timeout, that edits real files.
- **The watcher** is a second, much cheaper process (a small/fast model — Haiku is already
  wired into this codebase for summarization — is plenty) that:
  1. Polls the worker's real activity at a short interval (e.g. every 30–60s): the CLI's
     own session transcript file (`~/.pi/agent/sessions/...jsonl` for `pi`; find and
     document the equivalent for `qoder`), plus `git status --porcelain` / `git diff
     --stat` in the worker's `working_dir`, plus whether the subprocess PID is still alive.
  2. Turns that raw signal into one or two sentences a human (or the coordinator) can read
     without parsing JSONL — "editing sim/school/school_fixture.py, last tool call 40s ago,
     3 files touched so far" vs. "no tool call in 6 minutes, process still running" (the
     latter is exactly the shape of a real hang, and currently indistinguishable from
     healthy-but-slow).
  3. Writes that one-line status to a `run_dir/progress.json` (or appends to a
     `progress.log`) that a **new, non-blocking** MCP tool — call it `get_run_progress(run_id)`
     — can read at any time without waiting for the worker to finish.
  4. Optionally, pushes rather than waits to be pulled: if the host environment has a
     notification mechanism the MCP server can call into (worth checking before assuming
     this needs inventing), the watcher uses it at a sensible cadence (e.g. every 5
     minutes, or on a state change — new file touched, no activity for N minutes) instead
     of making the coordinator poll.

## Why this needs a second *process*, not just a smarter single call

The core problem is that `dispatch_worker`'s current shape is **one coroutine, one await,
one result** (`cli_adapter.py::run` — `asyncio.wait_for(_drain_and_wait(), timeout=timeout_s)`
with nothing in between). Anything that must happen *during* that wait — reading the
transcript file, checking git state, deciding "is this worth telling the coordinator about
right now" — cannot live inside that same coroutine without restructuring it anyway. Once
it's being restructured, running the observation loop as an actually-separate task (not
necessarily a separate OS process — an `asyncio.create_task` running concurrently with the
worker's own `_drain_and_wait` is enough) is barely more work than bolting a periodic
`git diff --stat` onto the timeout path, and gives you *continuous* visibility instead of
one snapshot at the only two points the current code already touches disk (start and
timeout-or-exit).

## Incremental path (don't build the whole thing at once)

1. **Prerequisite, cheap, do this regardless of the rest:** the two fixes already named in
   the findings doc — on timeout, run `git diff --stat`/`git status --porcelain` in
   `working_dir` and include it in the result; document in the tool schema that
   `raw_output_path` may be near-empty for `pi`/`qoder` in headless mode. This alone would
   have told the coordinator "4 files touched" instead of nothing, with zero architecture
   change.
2. **Make `dispatch_worker` non-blocking.** Return `{run_id, status: "running"}` immediately
   after spawning the subprocess (already backgrounded one layer up, by the calling
   harness, in the incident above — but cc-orchestrator itself still blocks the MCP call
   for up to `timeout_s`). Add `get_run_status(run_id)` / `get_run_progress(run_id)` for the
   coordinator to check in on. This is the structural prerequisite for everything below —
   without it, a watcher's output has nowhere to surface to mid-run.
3. **Add the watcher loop.** A background `asyncio.Task` started alongside the worker
   subprocess, polling transcript file + git state on an interval, writing to
   `run_dir/progress.json`. No LLM call yet — just structured facts (last-modified
   timestamp of the transcript, line count delta since last poll, `git diff --stat` output,
   process-alive check).
4. **Add the summarization layer.** Feed the watcher's structured facts through the
   existing Haiku summarizer path on each poll (or every Nth poll, to control cost) to
   produce the one-line human-readable status. This is the part that actually delivers
   "the monitor reports to you from time to time" rather than just "the data exists if you
   go look."
5. **(Optional, needs host support investigated first) Push instead of pull.** Only worth
   doing if whatever MCP host is in use actually has a notification channel the server can
   call into asynchronously; don't build this speculatively.

## Open questions to settle before implementing

- **Where does `qoder`'s equivalent of `pi`'s `~/.pi/agent/sessions/*.jsonl` live?** The
  existing findings doc only confirms this path for `pi`. Needs the same discovery pass for
  `qoder` before the watcher can be CLI-agnostic — may need a per-adapter
  `transcript_path(working_dir)` method alongside the existing per-adapter config.
  `WorkerAdapter`/`CliAdapter` already has the right shape (one adapter class per CLI) to
  hang this off of.
- **Polling interval vs. cost/noise.** Too frequent, and the watcher's own Haiku calls add
  up over a 50-minute dispatch; too infrequent, and it's barely better than the current
  start/end-only visibility. Start conservative (e.g. 60–90s) and tune against real
  dispatches.
- **What should `get_run_progress` return once the worker finishes?** Presumably it should
  converge on the same shape `dispatch_worker` already returns (`claims`, `status`, etc.) —
  worth designing the progress-poll result and the final result as the same type from the
  start, so the coordinator doesn't need two different code paths depending on whether it
  polled mid-run or waited for completion.
- **Does this replace or sit beside the blocking `dispatch_worker` call?** Recommend
  keeping both: a quick task (small review, a fast fix) has no need for the extra
  machinery; a long implementation dispatch (exactly the `pi` Story-11.7-shaped case this
  proposal is named after) is where it earns its cost.
