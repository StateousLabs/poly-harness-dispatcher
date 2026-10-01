# poly-harness-dispatcher (PHD)

Spawn headless AI coding agents — **Claude Code** (`claude -p`), **Codex** (`codex exec`) and
**OpenCode** (`opencode run`) — from one harness-neutral spec, and get back one normalized result.

> Layer 1 (core spawner) only: runs on the host with **no sandbox**. See `CoreDesign.md`.

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

## Tests

```bash
uv run pytest                    # offline: fakes + recorded streams
PHD_LIVE=1 uv run pytest tests/live -v   # real CLIs, costs a few cents
```
