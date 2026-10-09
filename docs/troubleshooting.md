# Troubleshooting

This page lists every `PhdError` with its cause and fix, the common `RunResult` error kinds with what to check,
and notes for Windows, Jupyter and free models.

## Pre-launch errors (`PhdError`)

All inherit from `phd.PhdError`; each message names the file, field or value involved.

| Error | Raised by | Cause | Fix |
|---|---|---|---|
| `UnknownHarness` | `Spec`, `Credentials.from_host`, `ModelCatalog` | harness not `claude`, `codex` or `opencode` | fix the name |
| `HarnessNotInstalled` | `start` / `spawn`, `ModelCatalog.models` | the CLI is not on `PATH` | install it, fix `PATH`, or pass `Dispatcher(executables={"claude": ["C:/path/claude.exe"]})` |
| `CwdNotFound` | `Spec` | `cwd` is not an existing directory | create it or fix the path |
| `InputNotFound` | `Inputs` | `brief` or `rules` is not a file (relative paths resolve against the Python process's current directory) | use an absolute path |
| `EmptyTask` | `Inputs` | no `brief` and blank `msg` | give a brief or a message |
| `StackInvalid` | `Stack.load` | malformed stack (missing `stack.json`, not exactly one `agents/*.md`, skill without `SKILL.md`, bad `.mcp.json` / `hooks.json`) | see [Stacks: validation errors](concepts/stacks.md#validation-errors) |
| `StackInvalid` | `phd.stacks` | bad manifest (unknown key, bad `name` / `version`, non-https scheme); a name clash without `replace = true`; a header that needs a missing `${env:NAME}` or expands to a value with a line break or NUL; an unsafe store name (not `[A-Za-z0-9][A-Za-z0-9._-]*`, ends in `.`, Windows device name); a store folder that exists but is not indexed; a corrupt `index.json`; a hook bundle named `hooks.json` or colliding with a hooks file; `${here}` in a single-file MCP source; a URL string passed to `resolve` (use `compose(name, version, base=url)`); the store lock held by another process for over 30 s ("stack store is busy"); a store path locked by another program or not writable | fix the manifest or the named entry; for a clash set `replace = true` on the entry; for a corrupt index fix or delete `<store>/index.json` ([Stack resolver](concepts/stack-resolver.md)) |
| `StackFetchError` | `phd.stacks.resolve` | a source path is missing; a download failed (HTTP error, timeout, a refused redirect: only https, or http to localhost, is followed); a component could not be copied into the stack; an archive is unusable or has an entry outside its folder; a required file (`stack.json`, `SKILL.md`, `.mcp.json`, `hooks.json`) is not in the source | check the path or URL (messages show URLs without query string or headers), the token in `headers`, and the archive layout |
| `StackConflict` | `phd.stacks.resolve` | `name@version` is already stored with different content | bump the version, or pass `replace=True` to overwrite |
| `StackNotFound` | `phd.stacks.get` / `remove`, `Dispatcher` (`plan` / `start` / `spawn`) | no stored stack with that ref; an indexed stack whose folder is missing ("folder missing from the store", run `stacks.prune()`); or the `Spec`'s stack folder was removed from the store after you resolved it | `stacks.list()` to see what exists; re-resolve the stack and build a new `Spec` |
| `StackIncompatible` | `Spec` | `stack.json` `harnesses` excludes `Spec.harness` | pick a listed harness or extend the list |
| `HookUnsupported` | `Stack.load`, `Dispatcher` | unknown hook event; event or tool not expressible for this harness (opencode: only `pre_tool`/`post_tool`; codex: only tool `shell`; `web_search` / `mcp:` only on claude) | change the hook or the harness ([Hooks](concepts/hooks.md)) |
| `MissingSecret` | `Spec` (also during materialize) | the stack uses `${env:NAME}` and `Spec.secrets` has no `NAME` | add `secrets={"NAME": "..."}`; host env vars are not consulted |
| `MissingCredentials` | `Credentials.*`, `Spec`, `plan` / `start` / `spawn` | auth file missing or unreadable, no `claudeAiOauth.accessToken`, missing opencode provider, credentials for another harness, unsupported kind | log in with the CLI (`claude auth login`, `codex login`, `opencode auth login`), or use `Credentials.claude_oauth_token`; on macOS see [Getting started](getting-started.md#2-authenticate-each-harness) |
| `UnsafeAutonomy` | `Spec` | `autonomy="full"` without `unsafe=True` | pass `unsafe=True` if you accept no isolation, or use `restricted` |
| `UnsupportedFeature` | `plan` / `start` / `spawn` with `session_id` | `Inputs.rules` on a resumed turn ("rules cannot be passed when resuming a session": rules are part of the session's system prompt) | resume without `rules`; the session's original rules are re-applied automatically (start a new session to change them) |
| `InvalidSpec` | `Spec` | blank `model`; bad `project_config` / `autonomy`; non-bool `harness_builtins` / `schema_repair`; `timeout_s` / `budget_usd` not positive or not finite (NaN, infinity); `idle_timeout_s` not a positive finite number; `output_schema` that is not a valid JSON Schema (the message names the problem and its path), including one with a `$ref` that does not resolve (`unresolvable $ref: ...`; remote references are never fetched) | fix the value |
| `InvalidRunsRoot` | `Dispatcher` | codex with `runs_root` under the system temp directory | use a `runs_root` outside `%TEMP%` / `/tmp` (the default `~/.phd/runs` is fine) |
| `NotResumable` | `plan` / `start` / `spawn` with `session_id` | no run with that session in this dispatcher's ledger ("no run with session"; a run that crashed mid-run is found through its `session.json`, so a session whose only run crashed can be resumed, and the crashed turn's cost appears in the next turn on claude); the crashed run was started with `resumable=False` ("was not resumable (resumable=False) and did not finish"); the session belongs to another harness ("belongs to"); its recorded home is not `<runs_root>/<run_id>/home` of a run in this ledger ("is not a run home") or is, or contains, a symlink / junction where PHD writes ("is a symlink or junction"); its run home was deleted, e.g. `resumable=False` ("no longer exists"); the session ran in another `cwd` ("ran in cwd"); the session's `run.json` was deleted ("has no run.json"); another live run is using the session right now ("is busy with run …") | resume with the same harness, `runs_root` and `cwd`; keep `resumable=True` on the run that starts the session; for "busy", wait for (or cancel) the named run — a lock left by a crashed process is taken over automatically, and `<home>/.phd-session.lock` can be deleted by hand if its pid was reused by another process |
| `ModelNotSupported` | `Dispatcher` | model not in the harness catalogue; `.hint` holds the closest match, `.supported` the list | use a listed model, or `validate_model=False` to skip the check |

`RuntimeError: spawn() cannot run inside an event loop` (not a `PhdError`): you called the blocking API
from Jupyter or async code. Use `await d.start(...)` (with `session_id=` to resume).

## Failed runs (`RunResult.error.kind`)

Files to look at: `result.stderr_path` (`stderr.log`), `result.events_path` (`events.jsonl`, including `raw`
harness lines and `log` events), `result.run_dir/run.json` (exact argv, redacted env, stdin), and the run home
(`result.home_dir`).

| `kind` (status) | Meaning | What to check |
|---|---|---|
| `auth` (`auth_failed`) | the harness reported a login/authorization problem | expired `claude_host_login` access token (run `claude` once interactively or switch to `claude setup-token`); stale codex/opencode auth file; the run does not see your shell's `ANTHROPIC_API_KEY` etc. by design |
| `model_unavailable` (`failed`) | model exists but this account cannot use it (for example `404 model does not exist or you do not have access`) | pick another model; the catalogue cannot detect entitlement |
| `billing` (`failed`) | out of credit (for example opencode `402 Insufficient account funds`) | top up or use another model / provider |
| `budget` (`budget_exceeded`) | this turn's cost passed `budget_usd` (for claude on a resumed turn: its cumulative figure minus the session's earlier cost) | raise the budget; `cost_usd` shows what this run spent, `session_cost_usd` (claude) the whole session |
| `timeout` (`timed_out`) | `timeout_s` elapsed | the last events in `events.jsonl`; a tool waiting for input; raise `timeout_s` |
| `idle_timeout` (`timed_out`) | the harness printed nothing for `idle_timeout_s` | claude and codex print nothing while a tool runs, so a long build or test trips a small value: set `idle_timeout_s` above your longest tool call (or leave it unset and rely on `timeout_s`); `events.jsonl` shows the last event before the silence |
| `schema` (`failed`) | the answer is not JSON (`final answer is not JSON`) or does not match `output_schema` (`<json path>: <message>`, secrets scrubbed, at most 500 characters) | `result.structured_output` holds the parsed value if there was one, and `result_text` the raw answer; the message names the failing path. Make the schema or prompt clearer, or set `schema_repair=True` for one corrected turn (then the message starts with `after one repair turn: ` if it still fails, and `warnings` has `schema repair turn used`) |
| `cancelled` (`cancelled`) | `cancel()` was called | |
| `harness_crash` (`failed`) | non-zero exit with no error in the stream | `error.message` has the last 2000 characters of stderr (secrets scrubbed); full text in `stderr.log` |
| `harness_error` (`failed`) | the harness reported an error PHD could not classify | `error.message`, `raw` lines in `events.jsonl` |
| `no_result` (`failed`) | exit 0 without a final answer or final event | `events.jsonl` (did the stream format change?), `stderr.log` |
| `stream_overflow` (`failed`) | a stdout line over 16 MiB | very large tool results |
| `phd_internal` (`failed`) | an exception inside PHD after launch | `error.message` has the exception; please report it |

Other symptoms:

| Symptom | Check |
|---|---|
| a shell command is refused under `restricted` | allow it via `harness_config` (claude `{"permissions": {"allow": ["Bash(echo:*)", "PowerShell(echo:*)"]}}`, opencode `{"permission": {"bash": {"echo *": "allow"}}}`) |
| `harness_config key '...' conflicts with a PHD-owned value` warning | your scalar was ignored; PHD keeps its own ([merge rules](concepts/spec-and-inputs.md#harness_config)) |
| codex run fails right after start with a config error | `--strict-config` rejects unknown keys; check `harness_config` |
| a hook never fires | the generated matcher in `home/settings.json` / `home/hooks.json` / `home/config/plugin/phd-hooks.js`; the resolved command in `home/phd/hooks.resolved.json`; run that command by hand with a sample payload on stdin |
| a hook should block but does not | for `pre_tool`, any non-zero exit blocks, so check the hook matches the tool (`tool:`) and that the event is `pre_tool`; other events never block ([exit codes](concepts/hooks.md#exit-codes)) |
| `orphan protection unavailable: <Type>: <msg>` warning | the run went ahead, but nothing ties the harness to your process: if your program is killed, the agent may keep running. Windows: the Job Object could not be created or the process assigned (for example it already runs in a job that forbids nesting); POSIX: the reaper process could not start (`... reaper did not start (...)`). Cancel and timeouts still kill the tree. (If the Windows harness cannot be resumed at all, the run fails and the same text is in `warnings`.) |
| `could not load the <harness> model catalogue` warning | the catalogue command failed; the model was not checked |
| run directories pile up | PHD never prunes `runs_root`; delete old `<run_id>/` folders yourself |

## Windows notes

- Claude Code runs shell commands through its `PowerShell` tool, so allow rules need `PowerShell(...)` as well as
  `Bash(...)`; PHD's `shell` hook matcher already covers both.
- codex runs hook commands through `cmd.exe /C`. A Python interpreter path with spaces is handled by an extra pair
  of quotes, verified against `cmd.exe` but not against real codex.
- codex refuses a `CODEX_HOME` under `%TEMP%`: keep `runs_root` elsewhere (`InvalidRunsRoot`).
- `.cmd` / `.bat` CLI shims are run through `COMSPEC /c`.
- Killing a run terminates its Job Object, which also reaches descendants of a harness that already exited, and the
  job is closed when the harness exits. Only if protection is unavailable (the `orphan protection unavailable`
  warning) does PHD fall back to `taskkill /T /F`, which cannot find the children of an exited CLI process; then look
  for stray `node` / MCP server processes.
- Run-home cleanup (`resumable=False`) retries on sharing violations; if a file stays locked, the result carries
  `could not remove run home ...` and the home is left behind.
- Paths with spaces in `runs_root` are supported (`${stack}` is quoted in hook commands).

## Jupyter: stale kernel

Jupyter keeps imported modules in memory. After `git pull` or editing `src/phd/`, **restart the kernel**, or
the notebook keeps running the old code. Use the project's `.venv` as the kernel
(`uv sync --extra dev --extra examples`), and the async API (`await d.start(...)`).

## Free models (opencode)

Free opencode models (for example `opencode/space-bunny-free`, used by the live tests and the notebook; earlier
`opencode/big-pickle`) are added and withdrawn over time. If `Dispatcher(...)` raises `ModelNotSupported`, or a
run fails with `model_unavailable` or `billing`, run `opencode models` and pick one that is listed now, or set
`PHD_LIVE_OPENCODE_MODEL` for the live tests.

---

**Next:** [API reference](reference/index.md) · **See also:** [Results and events](concepts/results-and-events.md), [Run lifecycle](concepts/run-lifecycle.md)
