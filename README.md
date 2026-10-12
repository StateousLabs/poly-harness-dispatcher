# poly-harness-dispatcher (PHD)

[![CI](https://github.com/StateousLabs/poly-harness-dispatcher/actions/workflows/ci.yml/badge.svg)](https://github.com/StateousLabs/poly-harness-dispatcher/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/poly-harness-dispatcher?cacheSeconds=3600)](https://pypi.org/project/poly-harness-dispatcher/)
[![Python](https://img.shields.io/pypi/pyversions/poly-harness-dispatcher?cacheSeconds=3600)](https://pypi.org/project/poly-harness-dispatcher/)
[![License](https://img.shields.io/pypi/l/poly-harness-dispatcher?cacheSeconds=3600)](LICENSE)

Spawn headless AI coding agents (**Claude Code** `claude -p`, **Codex** `codex exec`, **OpenCode** `opencode run`)
from one harness-neutral spec, and get back one normalized result.

You describe the agent once as a **stack** (identity, skills, MCP servers, hooks in a `config/` directory) and the
run as a **spec** (harness, model, working directory, credentials, options). PHD builds an isolated config home per
run, launches the CLI, parses its stream into events and a `RunResult`, and records everything under
`~/.phd/runs/<run_id>/`.

> Status: layer 1 (the core spawner) is built. It runs agents **on the host with no sandbox**; sandboxing, an API
> and other components are planned ([architecture](docs/architecture.md)).

## Install

Python 3.12+; the harness CLIs you use must be on `PATH`.

```bash
pip install poly-harness-dispatcher    # import name: phd
```

From a clone (development, with [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --extra dev        # or: uv venv && uv pip install -e ".[dev]"
uv run pytest -q           # offline test suite
```

## Quickstart

```python
from phd import Credentials, Dispatcher, Inputs, Spec, Stack

spec = Spec(
    harness="claude",                          # or "codex", "opencode"
    model="haiku",
    stack=Stack.load("tests/fixtures/stack"),  # the bundled test stack
    cwd="path/to/repo",
    credentials=Credentials.from_host("claude"),
    secrets={"ECHO_PREFIX": "phd-echo"},       # fills ${env:ECHO_PREFIX} in the stack
)
d = Dispatcher(spec)
result = d.spawn(Inputs(msg="List the names of your skills."))
print(result.status, result.cost_usd, result.result_text)
followup = d.spawn(Inputs(msg="Which one has a scripts/ folder?"), session_id=result.session_id)
```

To compose a stack from several sources (a base stack plus extra skills, MCP servers or hooks, local or `https://`)
and keep it in a local store, use `phd.stacks`:

```python
import phd.stacks as stacks

stack = stacks.resolve(stacks.compose("reviewer", "1.0.0", base="tests/fixtures/stack",
                                      skills=["path/to/extra-skill"]))
spec = Spec(harness="claude", model="haiku", stack=stack, cwd="path/to/repo",
            credentials=Credentials.from_host("claude"))
```

In async code, `d.run(...)` ties the run to a block: leaving it cancels a run that has not finished, and a harness
whose caller dies is killed too (Windows Job Object, POSIX reaper).

```python
async with d.run(Inputs(msg="List the names of your skills.")) as handle:
    async for ev in handle.events():
        print(ev.type)                  # run.started, session.started, message, ..., run.finished
    result = await handle.wait()
```

`session_id=` continues that harness session (the home is re-materialized from the current spec).
`spawn()` blocks; in Jupyter or async code use `await d.start(inputs)`, then
`async for ev in handle.events()` and `await handle.wait()` (see `examples/quickstart.ipynb`). A failed run does
not raise: check `result.status` and `result.error`. Problems detectable before launch raise a `PhdError`.

On macOS the Claude login lives in the Keychain: use `Credentials.claude_oauth_token(token)` with a token from
`claude setup-token` instead of `from_host("claude")`.

## Documentation

- [Documentation index](docs/index.md): what is built, what is planned, and every page
- [Getting started](docs/getting-started.md): install, authentication per harness, first agent, tests, dry runs
- [Stack resolver](docs/concepts/stack-resolver.md): compose and store stacks from several sources
- [Architecture](docs/architecture.md): package layout, data flow, roadmap components
- [API reference](docs/reference/index.md)
- [Design record](docs/CoreDesign.md)

Build the docs site locally: `uv sync --all-extras && uv run mkdocs serve`.
