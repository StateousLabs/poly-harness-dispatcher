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
| `StackIncompatible` | `Spec` | `stack.json` `harnesses` excludes `Spec.harness` | pick a listed harness or extend the list |
| `HookUnsupported` | `Stack.load`, `Dispatcher` | unknown hook event; event or tool not expressible for this harness (opencode: only `pre_tool`/`post_tool`; codex: only tool `shell`; `web_search` / `mcp:` only on claude) | change the hook or the harness ([Hooks](concepts/hooks.md)) |
| `MissingSecret` | `Spec` (also during materialize) | the stack uses `${env:NAME}` and `Spec.secrets` has no `NAME` | add `secrets={"NAME": "..."}`; host env vars are not consulted |
| `MissingCredentials` | `Credentials.*`, `Spec`, `plan` / `start` / `spawn` | auth file missing or unreadable, no `claudeAiOauth.accessToken`, missing opencode provider, credentials for another harness, unsupported kind | log in with the CLI (`claude auth login`, `codex login`, `opencode auth login`), or use `Credentials.claude_oauth_token`; on macOS see [Getting started](getting-started.md#2-authenticate-each-harness) |
| `UnsafeAutonomy` | `Spec` | `autonomy="full"` without `unsafe=True` | pass `unsafe=True` if you accept no isolation, or use `restricted` |
| `UnsupportedFeature` | `Dispatcher`; `plan` / `start` / `spawn` with `session_id` | `output_schema` with opencode; `Inputs.rules` on a resumed turn ("rules cannot be passed when resuming a session": rules are part of the session's system prompt) | drop `output_schema` or use claude / codex; resume without `rules`; the session's original rules are re-applied automatically (start a new session to change them) |
| `InvalidSpec` | `Spec` | blank `model`; bad `project_config` / `autonomy`; non-bool `harness_builtins`; `timeout_s <= 0`; `budget_usd <= 0` | fix the value |
| `InvalidRunsRoot` | `Dispatcher` | codex with `runs_root` under the system temp directory | use a `runs_root` outside `%TEMP%` / `/tmp` (the default `~/.phd/runs` is fine) |
| `NotResumable` | `plan` / `start` / `spawn` with `session_id` | no finished run with that session in this dispatcher's ledger ("no finished run with session"); the session belongs to another harness ("belongs to"); its run home was deleted, e.g. `resumable=False` ("no longer exists"); the session ran in another `cwd` ("ran in cwd"); the session's `run.json` was deleted ("has no run.json") | resume with the same harness, `runs_root` and `cwd`; keep `resumable=True` on the run that starts the session |
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
| `budget` (`budget_exceeded`) | cost passed `budget_usd` | raise the budget; `cost_usd` shows what was spent |
| `timeout` (`timed_out`) | `timeout_s` elapsed | the last events in `events.jsonl`; a tool waiting for input; raise `timeout_s` |
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
| `could not load the <harness> model catalogue` warning | the catalogue command failed; the model was not checked |
| run directories pile up | PHD never prunes `runs_root`; delete old `<run_id>/` folders yourself |

## Windows notes

- Claude Code runs shell commands through its `PowerShell` tool, so allow rules need `PowerShell(...)` as well as
  `Bash(...)`; PHD's `shell` hook matcher already covers both.
- codex runs hook commands through `cmd.exe /C`. A Python interpreter path with spaces is handled by an extra pair
  of quotes, verified against `cmd.exe` but not against real codex.
- codex refuses a `CODEX_HOME` under `%TEMP%`: keep `runs_root` elsewhere (`InvalidRunsRoot`).
- `.cmd` / `.bat` CLI shims are run through `COMSPEC /c`.
- Killing a run uses `taskkill /T /F`; if the CLI process has already exited, its children cannot be found and may
  keep running. Look for stray `node` / MCP server processes after a crash.
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
