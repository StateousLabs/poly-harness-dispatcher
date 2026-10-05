# API reference

This page explains how the public API is organised and where each name is documented.

Everything a user needs is importable from the top-level package: `from phd import ...` (the names in
`phd.__all__`), plus the `phd.runs` and `phd.stacks` modules. Internal packages (`phd.spec`, `phd.execution`, `phd.harnesses`,
`phd.materialize`, `phd.results`, `phd._util`) are implementation details and may change.

| Page | Names | Role |
|---|---|---|
| [Spec and stack](spec.md) | `Spec`, `Inputs`, `Credentials`, `Stack`, `HookDef` | describe a run: what, where, how, who, the task |
| [Stack resolver](stacks.md) | `phd.stacks`: `resolve`, `get`, `list`, `remove`, `prune`, `compose`, `load_manifest`, `Manifest`, `ComponentSource`, `StoreEntry` | build a `Stack` from several sources; the local stack store |
| [Dispatcher](dispatcher.md) | `Dispatcher`, `RunHandle` (and the `MaterializedHome` that `plan()` returns) | dry-run, launch, stream, wait, cancel, resume |
| [Results](results.md) | `RunResult`, `RunStatus`, `RunError`, `Usage`, `Event`, `LocalLedger`, `ResultSink`, `phd.runs` | what a run produced, and where it is recorded |
| [Models](models.md) | `ModelCatalog` | pre-launch model validation |
| [Errors](errors.md) | `PhdError` and its 19 subclasses | pre-launch failures |

Typical flow:

```python
import phd
from phd import Credentials, Dispatcher, Inputs, PhdError, RunStatus, Spec, Stack

try:
    spec = Spec(harness="codex", model="gpt-5.6-terra", stack=Stack.load("config"),
                cwd="repo", credentials=Credentials.from_host("codex"))
    d = Dispatcher(spec)                      # validates hooks/features/model
    result = d.spawn(Inputs(brief="task.md"))
except PhdError as e:                         # pre-launch problem
    raise SystemExit(f"cannot start: {e}")

if result.status is not RunStatus.SUCCEEDED:  # post-launch problem: data, not an exception
    print(result.error.kind, result.error.message, result.stderr_path)
print(phd.runs.get(result.run_id).cost_usd)
```

Each reference page starts with a hand-written summary (readable on GitHub). The full rendered reference below
it is generated from the docstrings by mkdocstrings and appears on the MkDocs site only.

`phd.__version__` is `"0.1.0"`.

---

**Next:** [Spec and stack](spec.md) · **See also:** [Concepts: Spec, Inputs, Credentials](../concepts/spec-and-inputs.md)
