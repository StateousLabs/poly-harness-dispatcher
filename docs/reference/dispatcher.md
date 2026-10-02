# Dispatcher

This page is the API reference for `Dispatcher` and `RunHandle`, plus the `MaterializedHome` returned by
`Dispatcher.plan`. Behaviour in context: [Run lifecycle](../concepts/run-lifecycle.md).

## Summary

### Dispatcher

`Dispatcher(spec, *, runs_root=None, sinks=(), catalog=None, validate_model=True, executables=None, allow_temp_runs_root=False)`
Validates the spec against its harness (hooks, features, model, codex runs root) and runs agents for it.

| Member | Purpose |
|---|---|
| `.plan(inputs, *, session_id=None) -> MaterializedHome` | dry run: write the run home and `run.json`, launch nothing |
| `await .start(inputs, *, session_id=None) -> RunHandle` | materialize and launch; returns once the process has started |
| `.spawn(inputs, *, session_id=None) -> RunResult` | blocking `start` + `wait`; `RuntimeError` inside a running event loop |

`session_id` (a previous `RunResult.session_id`) continues that harness session: PHD finds the session's latest
run in the ledger and re-materializes its home from this dispatcher's `Spec`, keeping the harness's session data.
`Inputs.rules` is not allowed then. See [Run lifecycle](../concepts/run-lifecycle.md#resume).
| `.spec` | the `Spec` |
| `.ledger` | the `LocalLedger` at `runs_root` |
| `.sinks` | `[ledger, *sinks]` |
| `.warnings` | pre-launch warnings, copied into every run |

### RunHandle

Returned by `start`; not constructed directly.

| Member | Purpose |
|---|---|
| `async for ev in .events()` | normalized `Event`s until `run.finished`; single consumer |
| `await .wait() -> RunResult` | the result; never raises for a failed run |
| `await .cancel() -> RunResult` | kill the process tree; status `cancelled` if still running |
| `.run_id` | this run's id |
| `.resumed_from` | for a resumed session, the run it was found in; else `None` |
| `.materialized` | the launched `MaterializedHome` |

### MaterializedHome

Not exported from `phd` (import path `phd.materialize.MaterializedHome`), but returned by `plan()` and available
as `RunHandle.materialized`.

| Field / method | Meaning |
|---|---|
| `run_id`, `run_dir`, `home` | ids and paths of the run |
| `argv` | harness command line (`argv[0]` is the bare CLI name) |
| `env` | variables PHD sets for the child; hidden from `repr()` |
| `stdin` | the first user message |
| `warnings` | pre-launch and materialization warnings |
| `redacted_env() -> dict` | `env` with secret-looking values replaced by `<redacted>` |

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.Dispatcher

::: phd.RunHandle

---

**Next:** [Results](results.md) · **See also:** [API overview](index.md), [Run lifecycle](../concepts/run-lifecycle.md)
