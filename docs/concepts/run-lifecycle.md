# Run lifecycle

This page follows one run from `Dispatcher(...)` to the ledger: validation, planning, materialization, launch,
stream parsing, result and sinks; the run-directory layout; the resume flow and what must persist for it; how
cancel, timeout, idle timeout, budget, stream overflow and interrupts end a run; how a harness is tied to its caller
(orphan protection); and the optional schema repair turn.

## Overview

```mermaid
sequenceDiagram
    autonumber
    participant U as caller
    participant D as Dispatcher
    participant M as materialize + adapter
    participant L as LocalLedger
    participant P as HarnessProcess
    participant H as RunHandle (pump task)
    U->>D: Dispatcher(spec, ...)
    D->>D: adapter.validate(spec), ModelCatalog.validate, codex temp-dir check
    U->>D: start(inputs) / spawn(inputs)  (session_id: see Resume)
    D->>D: stack.content_hash()
    D->>L: allocate() → run_id, run_dir
    D->>M: materialize(adapter, spec, inputs, run_id, run_dir)
    M->>M: mkdir home/, copy stack → home/stack/, assemble prompt
    M->>M: adapter writes config files, returns argv + env
    D->>L: write run.json (env redacted)
    D->>P: resolve argv[0] on PATH, start (own process group), attach the lifetime guard, write prompt to stdin
    D->>H: create pump task, return RunHandle
    H->>H: emit run.started
    loop each stdout line (within timeout_s; idle_timeout_s resets on each line)
        P-->>H: line
        H->>H: JSON → parser.feed → events (events.jsonl + queue)
        H->>H: first session id → emit session.started (once)
        H->>H: running cost > budget_usd? → kill tree
    end
    P-->>H: exit code
    H->>H: build RunResult (output_schema checked here)
    opt schema error and Spec.schema_repair
        H->>P: one resumed repair turn (same run)
    end
    H->>H: emit run.finished
    H->>H: resumable=False → delete home/
    H->>L: put(result) → result.json
    H->>H: extra sinks put(result)
    U->>H: wait() → RunResult
```

## Steps

| # | Step | Code | Can raise (pre-launch) |
|---|---|---|---|
| 1 | **Validate** in `Dispatcher.__init__`: adapter checks (hooks, warnings), claude model check, codex temp `runs_root`. The codex / opencode model catalogue is looked up lazily, in a worker thread, at the start of step 2 on the first run (once per process) | `execution/dispatcher.py` | `HookUnsupported`, `InvalidRunsRoot`; `ModelNotSupported` (claude here, codex from `start()` / `plan()`; an opencode miss is a warning) |
| 2 | **Plan**: on the first `start()` / `plan()` of the process for this `runs_root`, delete credential copies (codex `home/auth.json`) that a killed PHD process left in run homes no live run holds (each home's session lock is taken for the check, marked as a sweeper; a home a live run holds keeps its copy; links and homes outside `runs_root` are skipped, so nothing outside it is touched); with `session_id`, find and check the session ([Resume](#resume)); hash the stack (pinned now: this is what runs), allocate `<runs_root>/<run_id>/` | `Dispatcher._plan` | `UnsupportedFeature`, `NotResumable`, `OSError` |
| 3 | **Materialize**: create `home/` (or, [resuming](#resume), clear the PHD-owned paths of the session's home), copy the whole stack to `home/stack/`, assemble the prompt, let the adapter write its files and return `argv` and `env`; write `run.json` | `materialize/__init__.py`, adapters | `MissingCredentials`, `MissingSecret` |
| 4 | **Launch**: resolve `argv[0]` (or use `executables[harness]`), start the process in `Spec.cwd` with the stripped host env plus `env`, stdout/stderr piped, stdin written in the background; attach the [orphan-protection](#orphan-protection) guard | `RunHandle.launch`, `execution/process.py`, `execution/lifetime/` | `HarnessNotInstalled` |
| 5 | **Stream / parse**: read stdout line by line; non-JSON lines become `log` events; JSON objects go to the harness parser, which yields normalized events and accumulates the outcome; stderr is copied to `stderr.log` | `RunHandle._consume` | never (data) |
| 6 | **Result**: decide the error (precedence below), check the answer against `output_schema`, build `RunResult`; optionally run one [schema repair turn](#schema-repair-turn); emit `run.finished`; delete `home/` if `resumable=False`; delete the adapter's credential copies (codex `home/auth.json`) however the run ended, before the session lock is released (a failed deletion becomes a warning) | `RunHandle._build_result`, `_repair`, `_pump` | never (data) |
| 7 | **Sinks**: `LocalLedger.put` writes `result.json` atomically, then each extra sink | `RunHandle._pump` | never: sink errors become warnings |

`plan(inputs)` runs steps 2 and 3 only (and, for codex / opencode, the catalogue lookup, synchronously), then deletes
the credential copies again: a dry run's home has no codex `auth.json`. Note that a `HarnessNotInstalled` at step 4 leaves a run
directory with `run.json` and `home/` (without credential copies) but no `result.json`. A caller cancelled while `start()` is still planning leaves
nothing behind: the session lock is released and a fresh run's directory is removed (a resumed turn's shared home is kept).

### Never blocks the event loop

Every wait in the async API is an `await`. `start()` runs the model catalogue lookup, the home materialization (stack copy, skills, credential files), the session lock and the ledger writes in worker threads (`asyncio.to_thread`); the removal of a `resumable=False` home also runs in a worker thread. The Windows `taskkill` fallback of the process-tree kill is started without waiting for it. A ticker task on the same loop keeps beating while a run starts, runs and ends. What stays on the loop is small: appending events and writing `result.json`. The blocking wrappers (`spawn`, `plan`) are for code without a running loop.

## Using a run: `run()` vs `start()`

Both launch the same way and return a `RunHandle`; they differ in who ends the run.

```python
# run(): leaving the block ends the run
async with d.run(inputs) as handle:
    async for ev in handle.events():
        print(ev.type)
    result = await handle.wait()

# start(): you own the handle; wait() or cancel() it yourself
handle = await d.start(inputs)
result = await handle.wait()
```

| | `await d.start(inputs)` | `async with d.run(inputs) as handle` |
|---|---|---|
| returns | a `RunHandle` | a `RunHandle` (same object) |
| leaving the scope | nothing happens; the run continues until it finishes or you `cancel()` it | a run that has not finished is cancelled (status `cancelled`, tree killed, result in the ledger) |
| exception or task cancellation in the block | the run keeps going | the run is cancelled, then the exception continues |
| `wait()` | shielded: cancelling the awaiting task does not stop the run | the same, but the block's exit cancels the run anyway |

On exit `run()` issues the kill first and then **awaits** the run until it has finished (the harness is dead and the
result recorded) before the exception continues. If the awaiting task is cancelled again while it waits, `run()`
keeps waiting (the kill is already issued, so the wait is bounded) and re-raises `asyncio.CancelledError` once the
cancelled run has finished. `handle.done` is `True` once the result exists. Errors raised before launch
(`start`'s list) are raised on entering the block.

### Session id and `session.started`

`RunHandle.session_id` is `None` until the harness reports its session id; then it is set and PHD emits one
`session.started` event (`{"session_id": ...}`), exactly once per run. For a resumed turn (`session_id=` given) the
id is known before launch: `RunHandle.session_id` is set at once and `session.started` is emitted right after
`run.started`. The same id ends up in `RunResult.session_id`; a repair turn does not emit it again.

!!! note "Resuming after a crash"
    The moment `session.started` fires, PHD writes `<run_dir>/session.json` (`session_id`, `harness`, `cwd`, `home`,
    `stack`, `resumable`, `started_at`). If the supervisor dies mid-run, that run never writes `result.json`, but
    `start(..., session_id=)` still finds the session through this record, so a session whose **only** run crashed
    can be resumed. A finished run of the session always wins over a crashed one. The crashed turn reported no cost,
    so on claude its cost shows up in the next turn's cost (the baseline is `0.0`). A crashed run that was started
    with `resumable=False` is not resumed (`NotResumable`). Failing to write the record is a warning on the result.

### Idle timeout

`Spec.idle_timeout_s` (default `None`, off) ends a run whose harness prints no stdout line for that many seconds:
the tree is killed and the result has `error.kind == "idle"`, message `no output for idle_timeout_s=<n>`,
status `timed_out`. The timer restarts on **every** stdout line (blank and non-JSON lines included). It is
independent of `timeout_s`, which still bounds the whole run. Claude and codex print nothing while a tool runs, so
set `idle_timeout_s` above your longest expected tool call; a lower value kills healthy runs.

## Run directory

```
<runs_root>/<run_id>/          runs_root defaults to ~/.phd/runs; run_id = YYYYmmdd-HHMMSS-<6 hex> (local time)
  run.json                     launch record, written before launch (also by plan())
  session.json                 the session record, written when session.started fires (absent until then)
  events.jsonl                 one normalized Event per line
  stderr.log                   the harness's stderr, raw
  result.json                  the RunResult, written after the run
  home/                        the isolated config home (absent for resumed turns, which use the session's
                               home, and after resumable=False)
    .phd-session.lock          present only while a run is using this home (see Session lock below)
    .phd-session.lock.takeover empty file used as an OS lock while a stale lock is taken over; may linger
```

| File | Written by | Contents |
|---|---|---|
| `run.json` | `Dispatcher` (plan / start) | `run_id`, `harness`, `model`, `cwd`, `argv`, `env` (values of names matching `TOKEN`, `KEY`, `AUTH_CONTENT`, `SECRET`, `PASSWORD` replaced by `<redacted>`), `stdin` (the full first message), `warnings`, `session_id` (the session being resumed, else `null`), `resumed_from`, `home`, `rules_text` (the session's rules block content, or `null`) |
| `session.json` | `RunHandle` (via `LocalLedger.write_session`) | written when `session.started` fires: `session_id`, `harness`, `cwd`, `home`, `stack` (ref), `resumable` (the spec's), `started_at` (ISO); lets a run that crashed before `result.json` be resumed |
| `events.jsonl` | `RunHandle` | `{"run_id", "harness", "type", "data", "ts", "raw"}` per line ([events](results-and-events.md#events)) |
| `stderr.log` | `HarnessProcess` | unfiltered stderr bytes |
| `result.json` | `LocalLedger.put` | `RunResult.to_dict()` |
| `home/` | materialize + the harness | see [Harnesses](harnesses.md) |
| `hook.log` | **not PHD**: the test stack's `guard.py` writes it under `$PHD_RUN_DIR`; your hooks can do the same | |

## Resume

A run continues a harness session when `start`, `spawn` or `plan` get `session_id=` (a value from a previous
`RunResult.session_id`). Everything except the harness's own session data comes from the **new** `Spec`.

```mermaid
sequenceDiagram
    participant U as caller
    participant D as Dispatcher
    participant L as LocalLedger
    participant M as materialize + adapter
    U->>D: start(inputs, session_id=sid) / spawn(inputs, session_id=sid)
    D->>D: inputs.rules set? → UnsupportedFeature (nothing written)
    D->>L: find_session(sid) → SessionRef (latest finished run, else the newest crashed run's session.json)
    D->>D: found? same harness? home inside this ledger, no links? exists? same cwd? run.json? (session gone: a failed `session` run, nothing launched; tampering or busy: NotResumable, nothing written)
    D->>L: allocate() → new run_id, run_dir (no home/ of its own)
    D->>M: materialize(..., home=previous.home_dir)
    M->>M: delete PHD-owned paths in the home, write them again from the new Spec
    D->>D: argv = adapter.resume_argv(fresh argv, sid, spec); stack changed? → warning
    D->>L: write run.json (session_id, resumed_from = previous.run_id)
    D->>D: launch in spec.cwd with the shared home, stdin = first message from inputs
```

Checks, in this order, all made before anything is written. A check marked **`session` result** does not raise:
`start` / `run` / `spawn` return a finished, failed run with `error.kind == "session"` (status `failed`, reason in
`error.message`). That run gets a run id, a run directory with `run.json`, `events.jsonl` (`run.started`,
`run.finished`) and `result.json` (so it is listed by `phd.runs.list()`), reaches the sinks, and has `home_dir == ""`;
no harness is started, no home is created, no lock is taken. `plan()` always raises `NotResumable`. The other
checks (rules, containment, links, `run.json`, busy) raise, because they are caller errors or integrity problems.
If the harness itself reports that the session no longer exists (claude `No conversation found`, codex
`no rollout found for thread`, opencode `Session not found`), the run's error kind is `session` too.

| Check | Why | If it fails |
|---|---|---|
| `inputs.rules` is `None` | rules belong to the session: the ones it started with are re-applied on every turn | `UnsupportedFeature` ("rules cannot be passed when resuming a session") |
| a run in **this** dispatcher's ledger has that session: a finished run with `RunResult.session_id == session_id`, else a run whose `session.json` names it and that never wrote `result.json` (it crashed) | PHD finds the session's home through the ledger; the latest finished run (by `started_at`) is used, whatever its status; without one, the newest crashed run is | run with `error.kind == "session"` ("no run with session") |
| that run's harness is `spec.harness` | sessions are harness-specific | `session` result ("belongs to") |
| a run that never finished was not started with `resumable=False` | such a run asked for its home to be discarded | `session` result ("was not resumable (resumable=False) and did not finish") |
| its `home_dir` is `<runs_root>/<run_id>/home` of a run in this ledger (resolved; the run dir has a `run.json`) and not a symlink or junction | `home_dir` is read from `result.json`, and resuming deletes PHD-owned paths under it | `NotResumable` ("is not a run home", "is a symlink or junction") |
| its `home_dir` still exists | the home holds the harness session (claude `CLAUDE_CONFIG_DIR`, codex `CODEX_HOME`, opencode `XDG_DATA_HOME`) | `session` result ("no longer exists"); happens after `resumable=False` |
| no PHD-owned path in the home (or a parent of one inside it) is a symlink or junction | deletion must never leave the home | `NotResumable` ("is a symlink or junction") |
| its `cwd` is `spec.cwd` | claude keys sessions by working directory | `session` result ("ran in cwd") |
| the session run's `run.json` exists | it carries the session's `rules_text` | `NotResumable` ("has no run.json") |
| no live run holds the session's lock | two turns must never rewrite and use the same home at once | `NotResumable` ("is busy with run …"); the empty run directory that was just allocated is removed, the home is untouched |

### Session lock

Every run that uses a home holds `<home>/.phd-session.lock` (JSON: `pid`, `run_id`, `started_at`) for as long
as it runs:

- a **fresh run** takes it on its new, still empty home, before materialization (so no credential copy is ever in
  a home without a live lock; see the sweep below);
- a **resumed turn** takes it on the session's shared home after the checks above pass and **before** any
  PHD-owned path in the home is deleted or rewritten;
- `plan(...)`, with or without `session_id`, never takes or checks the lock of the home it previews (a dry run
  never touches the shared home). The first `start()` / `plan()` of a process for a `runs_root` does take, briefly,
  the locks of other run homes that still hold a leftover credential copy (the credential sweep, step 2).

The lock is created atomically (`O_CREAT | O_EXCL`) and released on every exit path (success, failure, timeout,
budget, cancel, interrupt, and when the harness cannot be started); a fresh run with `resumable=False` releases it
before its home is deleted. A lock whose process is no longer running, or whose file cannot be read, is **stale**
and is taken over automatically; the takeover (re-check, then delete) runs under an OS lock on
`<home>/.phd-session.lock.takeover` (waits up to 5 s, then `NotResumable` "busy"), so two acquirers that see the same
stale lock cannot both win. That file is never written to or deleted and holds nothing. The credential sweep holds
a lock marked `"sweep": true` for one delete; an acquirer that meets a live sweeper, or a lock file that is still
being written, waits up to 2 s for it before it reports busy, so a sweep in another process delays a resume but does
not fail it (a 0.1.0 process does not know the marker and reports busy at once). Several runs can share one
Python process (async), so for a lock written by this process PHD checks its in-process record of runs that still hold a lock rather than the pid. In practice the lock
matters for turns of an existing session: a session only becomes resumable once a run of it has finished (its
`result.json` exists), so the lock prevents two resumed turns (from one process or several) from running at once.

Details:

- Each resumed turn gets a **new** `run_id` and run directory (`run.json`, `events.jsonl`, `stderr.log`,
  `result.json`) but **no** `home/` of its own: `RunResult.home_dir` is the shared home, and `resumed_from` is
  the run the session was found in (for a chain, the previous turn). Chains work because the lookup always takes
  the latest run of the session.
- The first message is built from `inputs` exactly as for a fresh run (`# Task brief` and/or `# Message`), so a
  resumed turn can carry a brief.
- **Rules persist for the session's lifetime.** The fresh run that starts a session records its rules block as
  `rules_text` in `run.json` (the stripped rules file text, or `null`), and every resumed turn copies it forward
  into its own `run.json`. The system prompt of a resumed turn is the current stack's identity, then the
  session's rules block, then project context per the new `Spec.project_config`, exactly as a fresh run lays it
  out. So codex and opencode, which re-read `AGENTS.md` / `instructions` on every launch, behave like claude.
- `argv` is this materialization's fresh argv plus the harness's resume flag (claude `--resume <sid>`, opencode
  `--session <sid>`; codex rebuilds `codex exec resume <sid> ...` from the fresh flags, without `-C`). Resume flags
  never accumulate along a chain.
- `stack_hash` is the hash of the stack in the new `Spec` (the home's stack copy is replaced). If the stack ref
  (`name@version`) differs from the previous run's, the result gets a warning; the turn still runs.
- **Cost is per turn.** claude reports the session's cumulative cost on `--resume`; PHD subtracts the session's
  cost before this turn (the previous run's `session_cost_usd`, falling back to its `cost_usd` for an older record),
  so `RunResult.cost_usd` and the `budget_usd` check cover this turn only, and `session_cost_usd` holds claude's
  cumulative figure. codex and opencode report per-invocation cost and are unchanged.
- A resumed run never deletes the shared home, even if `Spec.resumable` is `False` (and its `resumable` is `True`
  whenever it has a session id). A fresh run with `resumable=False` still deletes its home, ending the chain.
- `plan(inputs, session_id=...)` previews a resumed turn without modifying the session: the checks run, the
  session's rules are applied and `argv` carries the resume flag, but everything is written into the plan's own
  `<run_dir>/home`; the shared home is never touched (so the preview's `env` points at the preview home).

### What a resumed turn reuses vs takes from the new Spec

| Reused from the session | Taken from the new `Spec` (and `Inputs`) |
|---|---|
| the harness's session data in the home: claude `projects/`, `.claude.json` and anything else Claude writes; codex `sessions/`, `*.sqlite`, logs; opencode `data/`, `state/` and what it installs under `config/` | everything PHD writes: the stack copy (`home/stack/`), the hook runtime (`home/phd/`, incl. `shim_config.json`), skills, the system-prompt / `AGENTS.md` file (identity and project context; the rules block is the session's), MCP config, `settings.json` / `config.toml` / `opencode.json` (autonomy, permissions, builtin suppression, merged `harness_config`), codex `hooks.json` and `output-schema.json`, opencode's hook plugin, credentials |
| the session's rules (`rules_text`, re-applied to the system prompt) | the whole argv (model, autonomy flags, budget flag, schema) and env, built fresh |
| | `cwd` (must equal the session's), `timeout_s`, `budget_usd`, `secrets`, `runs_root`, sinks |

PHD-owned directories (skills, `stack/`, `phd/`) are deleted and copied again, so a skill or hook removed from the
stack disappears; PHD-owned files are overwritten or removed. Nothing else in the home is touched. Rules cannot be
changed on a resumed turn (start a new session for new rules); the session's rules are written back into the
system prompt every time. A changed identity or project context does take effect in the rewritten file; how a
harness treats a system prompt that changes mid-session is up to the harness.

Hooks on a resumed turn log to the **new** run: `shim_config.json` is rewritten, so hooks receive the new
`run_id` and `PHD_RUN_DIR`.

## How a run ends

| Trigger | What PHD does | `error.kind` / status |
|---|---|---|
| harness exits normally | waits for exit, builds the result from the parser outcome | none → `succeeded`, or the parser's error |
| `await handle.cancel()` | sets a flag, kills the process tree, waits | `cancelled` / `cancelled` |
| `timeout_s` elapses (measured from the start of streaming; covers the stdin write) | kills the tree, waits | `timeout` / `timed_out` |
| no stdout line for `idle_timeout_s` | kills the tree, waits | `idle` / `timed_out` |
| streamed cost > `budget_usd` | kills the tree as soon as the parser's running cost exceeds the budget (opencode: after any `step_finish`; claude: only at its final `result` line, since that is where it reports cost) | `budget` / `budget_exceeded` |
| a stdout line longer than 16 MiB | marks overflow, kills the tree | `stream_overflow` / `failed` |
| an exception inside PHD while streaming or building the result | kills the tree, still returns a (minimal) result | `phd_internal` / `failed` |
| the answer does not match `output_schema` (and no other error applies) | none, or one [repair turn](#schema-repair-turn) if `schema_repair=True` | `schema` / `failed` |
| the pump task is cancelled or interrupted (`KeyboardInterrupt`, `asyncio.CancelledError`, e.g. Ctrl-C during `spawn()`) | kills the tree if still running and re-raises; **no result is built and no `result.json` is written** | none |

Precedence when several apply is listed in [Results and events](results-and-events.md#error-kinds-and-precedence).
`RunHandle.wait()` is shielded: cancelling the task that awaits it does not stop the run; call `cancel()`.

## Process-tree kill

The harness is started in its own process group so the whole tree (the CLI, its MCP servers, shell commands) can
be killed by `cancel()`, `timeout_s`, `idle_timeout_s`, budget or stream overflow.

| | Windows | POSIX |
|---|---|---|
| start | `CREATE_NEW_PROCESS_GROUP`, created suspended and put in a Job Object before it runs | `start_new_session=True`, then a reaper process is started |
| kill | `TerminateJobObject`: reaches every process in the job, including orphaned descendants. Without a job (protection unavailable): `taskkill /PID <pid> /T /F`, then `Process.kill()` if that failed | `os.killpg(pid, SIGKILL)` |

Waiting for exit polls the return code every 50 ms instead of `Process.wait()`, because a descendant that still
holds stdout/stderr open would otherwise hang the wait; the stderr copier then gets up to 5 s to finish.

### Stop bound

Leaving `async with d.run(...)` (cancellation, an exception, or the end of the block) returns in about 5 s at worst,
plus the time any user-supplied result sinks take. The harness is never asked to stop: it is force-killed
(`TerminateJobObject` / `SIGKILL`), so a harness that ignores `SIGTERM`, `SIGINT` and Ctrl+Break still dies at once
(measured: under 0.1 s in the test suite). The 5 s is the stderr grace period. On Windows the job is closed first, so
every descendant is already dead and there is no stderr wait. On POSIX the wait applies only to a descendant that
escaped the kill (for example by starting its own session) and still holds stderr open.

## Orphan protection

Killing on cancel or timeout needs PHD to be alive. Orphan protection covers the other case: **the caller dies**
(crash, `kill -9`, closed terminal) and must not leave an agent running. Each platform uses a different mechanism,
and they are not equivalent.

| | Windows | POSIX |
|---|---|---|
| mechanism | a Job Object with kill-on-close | a small reaper process (`execution/lifetime/reaper_main.py`, stdlib only) |
| when the caller dies | the OS closes PHD's job handle and kills every process in the job | the reaper sees EOF on its stdin and sends `SIGKILL` to the harness's process group |
| setup | the harness is created **suspended**, assigned to the job, then resumed: there is no window in which it runs unprotected | the reaper starts right **after** the harness, so there is a short window before it is up |
| `cancel()` / timeouts | terminate the job, reaching orphaned descendants | `killpg` on the harness's group |
| normal finish | the job is closed as soon as the harness exits, **before** the stderr grace period, so leftover descendants (for example a background process a tool left running) are killed at once | PHD tells the reaper `exit` and it leaves quietly; background processes a tool left running are **not** killed (the reaper acts only if the caller dies) |
| escape | children that ask to break away from the job are refused | descendants that start their own session (`setsid`) leave the harness's process group and escape the reaper |

POSIX details: the reaper runs in its own session (a terminal Ctrl+C aimed at the caller's group must not take it
too) and reads its stdin: a line `exit` means "the run ended normally" (leave quietly), EOF without it means "the
caller died". It also exits by itself once the harness's group is gone. PHD reaps the reaper in a background thread,
so finishing a run never blocks the event loop.

**When protection cannot be set up** the run continues, unprotected, and `RunResult.warnings` gets
`orphan protection unavailable: <Type>: <msg>` (on POSIX: `orphan protection unavailable: reaper did not start
(<Type>: <msg>)`). Two cases are handled differently:

- If the guard crashes outright, `start()` kills the harness and raises, so a harness is never left running that
  nothing owns.
- On Windows, a suspended harness that cannot be resumed is terminated and the run fails; the reason is in
  `warnings`.

## Schema repair turn

With `Spec.output_schema` and `Spec.schema_repair=True`, a run whose answer fails the schema check
(`error.kind == "schema"`) gets **one** more turn in the same run: PHD resumes the harness session, emits a
`schema.repair` event (`{"error": ...}`) once the repair process has started, and sends the prompt `Your final answer did not match the required
JSON Schema: <error>. Reply with only the corrected JSON.`

- The repair turn is a second process of the **same run**: same `run_id`, run directory, `events.jsonl` and
  `RunResult`. It is not a new run and emits no second `session.started`.
- It shares the run's limits: the same `timeout_s` deadline, `idle_timeout_s` per turn, and the budget. Claude's
  repair process gets `--max-budget-usd` set to the **remaining** budget (`budget_usd` minus what the first turn
  cost); the budget check counts the first turn's cost plus the repair turn's own (claude's cumulative figure
  minus what the first turn reported). If nothing remains, no repair runs and the first result is returned with
  the warning `schema repair skipped: budget_usd spent`.
- It is skipped when the run was cancelled, when the first turn reported no session id, and when less than one
  second of `timeout_s` remains (warning `schema repair skipped: timeout_s nearly spent`).
- Cost, usage and turns are **summed** over both turns (each turn's own cost). If only one turn reports a cost,
  `cost_source` is `"unknown"` (a sum would undercount).
- The result gets the warning `schema repair turn used`. If the repaired answer still fails, the error is a
  `schema` error whose message starts with `after one repair turn: `. Any other failure of the repair turn
  (timeout, crash) is reported as usual.
- If the repair process cannot be started, the first turn's result is kept with the warning
  `schema repair turn could not start: <Type>: <msg>`. Any other unexpected error in the repair step keeps the
  first turn's result with the warning `schema repair turn failed: <Type>: <msg>`.

Messages and per-harness mechanisms: [Option combinations](combinations.md#output_schema).

## Stateous contract

The downstream user Stateous lists requirements B1 to B10. Each is proven by a test; B5, B6, B7, B8 and B10 run
live, once per harness (`PHD_LIVE=1 uv run pytest tests/live/test_stateous_contract.py -v`, a few cents).

| # | Requirement | Offline test (fake harness) | Live test (per harness: `[claude]`, `[codex]`, `[opencode]`) |
|---|---|---|---|
| B1 | Leaving `async with` stops the run | `tests/execution/test_lifecycle.py`: `test_leaving_the_block_without_wait_cancels`, `test_exception_in_the_block_cancels_and_reraises`, `test_outer_cancellation_while_waiting_cancels_run`, `test_outer_cancellation_during_events_cancels_run` | |
| B2 | The stop is fast | `tests/execution/test_fast_stop.py`: `test_cancelled_task_stops_fast`, `test_exception_in_block_stops_fast` | |
| B3 | The harness dies with the caller | `tests/execution/test_lifetime.py`: `test_harness_and_grandchild_die_with_their_caller` | |
| B4 | The event loop is never blocked | `tests/execution/test_no_loop_blocking.py`: `test_ticker_keeps_beating_through_a_run` | |
| B5 | `handle.session_id` is set at start | `tests/execution/test_lifecycle.py`: `test_session_started_is_emitted_once` | `test_b5_session_id_at_start` |
| B6 | `session_id=X` resumes X; unknown gives `kind="session"` | `tests/execution/test_session_result.py`, `tests/execution/test_crash_resume.py` | `test_b6_unknown_session`, `test_b6_resume_after_crash` |
| B7 | `output_schema` with a root `oneOf` works | `tests/execution/test_output_schema_dispatch.py`, `tests/harnesses/test_schema_translation.py` | `test_b7_root_oneof` |
| B8 | A schema mismatch gives `kind="schema"` | `tests/execution/test_structured.py`: `test_native_structured_output_that_does_not_match` | `test_b8_schema_mismatch` (a successful answer validates against the original schema) |
| B9 | `idle_timeout_s` stops a silent run | `tests/execution/test_lifecycle.py`: `test_idle_limit_stops_a_silent_run` | |
| B10 | `Event.summary` is one line, at most 500 characters | `tests/harnesses/test_summaries.py` | `test_b10_summaries` |

The live tests are in `tests/live/test_stateous_contract.py`; the crash test kills a caller subprocess
(`tests/live/live_crash_caller.py`) mid-turn and resumes its session from a new process.

## Host environment stripping

The child environment is `os.environ` minus every variable whose upper-cased name starts with `ANTHROPIC_`,
`CLAUDE_`, `CODEX_`, `OPENAI_`, `OPENCODE_` or `XDG_`, then updated with the adapter's `env`. Everything else
(`PATH`, `HOME`, `USERPROFILE`, other tokens such as `GITHUB_TOKEN`) is passed through. See
[Security and isolation](security-and-isolation.md).

---

**Next:** [Results and events](results-and-events.md) · **See also:** [Harnesses](harnesses.md), [Troubleshooting](../troubleshooting.md)
