# poly-harness-dispatcher (PHD)

Spawn headless AI coding agents — **Claude Code** (`claude -p`), **Codex** (`codex exec`) and
**OpenCode** (`opencode run`) — from one harness-neutral spec, and get back one normalized result.

> Layer 1 (core spawner) only: runs on the host with **no sandbox**. See `docs/CoreDesign.md`.

## Install

```bash
uv venv && uv pip install -e ".[dev]"
```

## A stack is a config dir

```
config/
  stack.json          {"name": "...", "version": "...", "harnesses": ["claude", "codex", "opencode"]}
  agents/AGENTS.md    the single agent's identity
  skills/<name>/      SKILL.md + references/ + scripts/
  .mcp.json           {"mcpServers": {...}}   ${stack} and ${env:NAME} placeholders allowed
  hooks/hooks.json    {"hooks": [{"event": "pre_tool", "match": {"tool": "shell"}, "command": "..."}]}
```

## Use

```python
from phd import Credentials, Dispatcher, Inputs, Spec, Stack

spec = Spec(
    harness="claude", model="sonnet",
    stack=Stack.load("path/to/config"),
    cwd="path/to/repo",
    credentials=Credentials.from_host("claude"),
    secrets={"ECHO_PREFIX": "hi"},
)
result = Dispatcher(spec).spawn(Inputs(brief="brief.md", rules="RULES.md", msg="focus on tests"))
print(result.status, result.cost_usd, result.session_id)

followup = Dispatcher(spec).resume(result, "now fix the failing test")
```

Async: `handle = await Dispatcher(spec).start(inputs)`, then `async for ev in handle.events()`,
`await handle.wait()` or `await handle.cancel()`.

Every run is recorded under `~/.phd/runs/<run_id>/` (`run.json`, `result.json`,
`events.jsonl`, `stderr.log`, `home/`); query with `phd.runs.list()` / `phd.runs.get(run_id)`.

## Defaults and safety

- `project_config="ignore"`: the repo's `CLAUDE.md` / `AGENTS.md` / project skills are not loaded.
- `harness_builtins=False`: the agent sees only its stack's skills. Claude's bundled skills and builtin plugins,
  codex's `.system` skills and opencode's `customize-opencode` are suppressed (claude's `design` and `doctor` still
  list but are denied). Pass `harness_builtins=True` to keep the harness defaults.
- `autonomy="restricted"`; `autonomy="full"` bypasses every harness permission check and requires `unsafe=True`.
- `timeout_s=1800` (covers writing the prompt too); `resumable=True` keeps the run home so `resume()` works.
- `Dispatcher(spec).plan(inputs)` is a dry run: it writes the run home and returns the `MaterializedHome`
  (argv, env, stdin, warnings) without launching anything.
- Hook commands (`hooks/hooks.json`) may use `${stack}`, the run's read-only copy of the stack; it is shell-quoted,
  so paths with spaces are safe.
- `Credentials.from_host("claude")` reads `~/.claude/.credentials.json`. On macOS the login lives in the
  Keychain, so that file is absent: use `Credentials.claude_oauth_token(token)` with a token from `claude setup-token`.

## RunResult

`status` (`succeeded | failed | timed_out | budget_exceeded | cancelled | auth_failed`), `error.kind`
(`auth`, `model_unavailable`, `billing`, `budget`, `timeout`, `cancelled`, `harness_error`, `harness_crash`,
`no_result`, `stream_overflow`, `phd_internal`), `result_text`, `cost_usd` with `cost_source`
(`harness | estimated | unknown`), `usage`, `session_id`, `run_dir`.

## Tests

```bash
uv run pytest                    # offline: fakes + recorded streams
PHD_LIVE=1 uv run pytest tests/live -v   # real CLIs, costs a few cents
```
