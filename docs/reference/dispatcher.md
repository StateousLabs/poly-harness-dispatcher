# Dispatcher

This page is the API reference for `Dispatcher` and `RunHandle`, plus the `MaterializedHome` returned by
`Dispatcher.plan`. Behaviour in context: [Run lifecycle](../concepts/run-lifecycle.md).

## Summary

### Dispatcher

`Dispatcher(spec, *, runs_root=None, sinks=(), catalog=None, validate_model=True, executables=None, allow_temp_runs_root=False)`
Validates the spec against its harness (hooks, features, model, codex runs root) and runs agents for it.

| Member | Purpose |
|---|---|
| `.plan(inputs) -> MaterializedHome` | dry run: write the run home and `run.json`, launch nothing |
| `await .start(inputs) -> RunHandle` | materialize and launch; returns once the process has started |
| `.spawn(inputs) -> RunResult` | blocking `start` + `wait`; `RuntimeError` inside a running event loop |
| `await .start_resume(previous, msg) -> RunHandle` | continue a previous run's session (`previous` = `RunResult` or run id) |
| `.resume(previous, msg) -> RunResult` | blocking `start_resume` + `wait` |
| `.spec` | the `Spec` |
| `.ledger` | the `LocalLedger` at `runs_root` |
| `.sinks` | `[ledger, *sinks]` |
| `.warnings` | pre-launch warnings, copied into every run |

### RunHandle

Returned by `start` / `start_resume`; not constructed directly.

| Member | Purpose |
|---|---|
| `async for ev in .events()` | normalized `Event`s until `run.finished`; single consumer |
| `await .wait() -> RunResult` | the result; never raises for a failed run |
| `await .cancel() -> RunResult` | kill the process tree; status `cancelled` if still running |
| `.run_id` | this run's id |
| `.resumed_from` | previous run id, or `None` |
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
