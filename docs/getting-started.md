# Getting started

This page covers installing PHD, authenticating each harness, running a first agent from a script and from the
quickstart notebook, running the test suite, and doing a dry run with `plan()`.

## 1. Install

To use PHD in your own project, install it from PyPI (the import name is `phd`):

```bash
pip install poly-harness-dispatcher     # or: uv add poly-harness-dispatcher
```

The rest of this section is for working on PHD itself from a clone.

PHD needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Its one runtime dependency is `jsonschema` (installed for you); the harness CLIs
you want to use must be installed separately and be on `PATH` (`claude`, `codex`, `opencode`).

```bash
git clone https://github.com/StateousLabs/poly-harness-dispatcher
cd poly-harness-dispatcher
uv sync --extra dev                 # package + pytest
uv sync --extra dev --extra examples  # + ipykernel for the notebook
uv sync --all-extras                # + mkdocs for building these docs
```

Extras defined in `pyproject.toml`:

| Extra | Contents | Needed for |
|---|---|---|
| `dev` | `pytest`, `pytest-asyncio`, `pytest-cov` | the test suite |
| `examples` | `ipykernel` | `examples/quickstart.ipynb` |
| `docs` | `mkdocs-material`, `mkdocstrings[python]` | `uv run mkdocs serve` / `build` |

## 2. Authenticate each harness

PHD never runs a login itself. It takes a [`Credentials`](concepts/spec-and-inputs.md#credentials) value and
injects it into each run's own environment or config home. Two styles:

| Harness | Host login (reuse the CLI's own login) | Token / key |
|---|---|---|
| claude | `claude auth login`, then `Credentials.from_host("claude")` (= `claude_host_login()`, reads `~/.claude/.credentials.json`) | `Credentials.claude_oauth_token(token)` with a token from `claude setup-token` (no Chrome), or `Credentials.anthropic_api_key(key)` |
| codex | `codex login`, then `Credentials.from_host("codex")` (= `codex_auth_file()`, `~/.codex/auth.json`) | none (only the auth file kind exists) |
| opencode | `opencode auth login`, then `Credentials.from_host("opencode")` (= `opencode_auth_file()`, `~/.local/share/opencode/auth.json`) | none; to copy only some provider entries use `Credentials.opencode_auth_file(providers=[...])` (`from_host` takes no `providers`) |

> **macOS keychain caveat.** On macOS Claude Code keeps its login in the Keychain, so
> `~/.claude/.credentials.json` does not exist and `Credentials.from_host("claude")` raises
> `MissingCredentials`. Use `Credentials.claude_oauth_token(token)` with a token from `claude setup-token`.

`claude_host_login` copies the whole login file into the run home for the turn and deletes it afterwards, like
`codex_auth_file`. It is the only claude credential that works with `Spec(chrome=True)` (Claude in Chrome). Details:
[Credentials](concepts/spec-and-inputs.md#credentials).

## 3. A stack

A stack is the agent: one identity file plus skills, MCP servers and hooks, in a directory. The repository ships
a test stack you can use right away: `tests/fixtures/stack/` (agent `helper`, skills `demo-minimal` and
`demo-notes`, a stdio MCP server `echo` that needs the secret `ECHO_PREFIX`, and a `pre_tool` hook on `shell`).
Layout and rules: [Stacks](concepts/stacks.md).

## 4. First agent, as a script

```python
# first_agent.py  (run from the repo root:  uv run python first_agent.py)
from pathlib import Path

from phd import Credentials, Dispatcher, Inputs, Spec, Stack

workdir = Path("scratch-workdir")
workdir.mkdir(exist_ok=True)

spec = Spec(
    harness="claude",
    model="haiku",
    stack=Stack.load("tests/fixtures/stack"),
    cwd=workdir,
    credentials=Credentials.from_host("claude"),
    secrets={"ECHO_PREFIX": "phd-echo"},   # fills ${env:ECHO_PREFIX} in the stack's .mcp.json
    timeout_s=300,
)
d = Dispatcher(spec)
print("warnings:", d.warnings)

result = d.spawn(Inputs(msg="List the names of your skills, nothing else."))
print(result.status, result.cost_usd, result.cost_source)
print(result.result_text)
if result.error:
    print(result.error.kind, result.error.message, "->", result.stderr_path)

followup = d.spawn(Inputs(msg="Which of those skills has a scripts/ folder?"), session_id=result.session_id)
print(followup.status, followup.result_text)
```

`spawn()` blocks. Passing `session_id=` continues that harness session with the same (or a changed) `Spec`;
`Inputs.rules` cannot be passed on a resumed turn ([Resume](concepts/run-lifecycle.md#resume)). A failed run does not raise: check `result.status` and `result.error`
([Results and events](concepts/results-and-events.md)). Problems PHD can detect before launch raise a `PhdError`
subclass ([Troubleshooting](troubleshooting.md)).

To switch harness, change `harness`, `model` and `credentials` together, for example
`harness="opencode", model="opencode/space-bunny-free", credentials=Credentials.from_host("opencode")`
(free opencode models change over time; check `opencode models`). For codex keep `runs_root` outside the
system temp directory ([why](concepts/harnesses.md#codex)).

## 5. First agent, in Jupyter (`examples/quickstart.ipynb`)

The notebook walks the same steps: pick a harness, load the test stack, build the `Spec`, dry run with
`plan()`, run with streaming events, resume, and list the ledger.

```bash
uv sync --extra dev --extra examples
# open examples/quickstart.ipynb and select the project's .venv kernel
```

Jupyter already runs an event loop, so `spawn()` raises `RuntimeError` there. Use the async API:

```python
handle = await dispatcher.start(inputs)
async for ev in handle.events():
    print(ev.type, ev.data)
result = await handle.wait()

followup = await (await dispatcher.start(Inputs(msg="next message"), session_id=result.session_id)).wait()
```

After updating PHD (`git pull`), restart the kernel: a running kernel keeps the old imported modules.

## 6. Dry run with `plan()`

`Dispatcher.plan(inputs)` allocates a run directory, writes the complete run home and `run.json`, and returns a
`MaterializedHome` without launching anything. Use it to inspect exactly what a harness would get.

```python
plan = Dispatcher(spec).plan(Inputs(msg="hello"))
print(plan.argv)            # the full command line
print(plan.redacted_env())  # variables PHD sets; secrets shown as <redacted>
print(plan.stdin)           # the first user message
for p in sorted(plan.home.rglob("*")):
    print(p.relative_to(plan.home))
```

A planned run has no `result.json`, so it never shows up in `phd.runs.list()`; its directory stays under
`runs_root` until you delete it.

## 7. Run the tests

```bash
uv run pytest -q                       # offline: fake harness + recorded streams, free
```

The live tests run the real CLIs against the test stack and cost a few cents (claude haiku) or nothing (opencode
free model). They write run directories to `.phd-test-runs/` in the repo (not `%TEMP%`, which codex refuses).

```bash
PHD_LIVE=1 uv run pytest tests/live -v                       # bash
$env:PHD_LIVE = "1"; uv run pytest tests/live -v             # PowerShell
```

Override the models with `PHD_LIVE_CLAUDE_MODEL`, `PHD_LIVE_CODEX_MODEL`, `PHD_LIVE_OPENCODE_MODEL`
(defaults: `haiku`, `gpt-5.6-terra`, `opencode/space-bunny-free`).

## 8. Build these docs

```bash
uv sync --all-extras
uv run mkdocs serve            # http://127.0.0.1:8000
uv run mkdocs build --strict   # output in site/
```

---

**Next:** [Stacks](concepts/stacks.md) · **See also:** [Spec, Inputs, Credentials](concepts/spec-and-inputs.md), [Troubleshooting](troubleshooting.md)
