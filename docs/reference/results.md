# Results

This page is the API reference for `RunResult`, `RunStatus`, `RunError`, `Usage`, `Event`, `LocalLedger`,
`ResultSink` and `phd.runs`. Semantics (precedence, events per harness, cost source):
[Results and events](../concepts/results-and-events.md).

## Summary

### RunResult

Dataclass with 34 fields (`run_id`, `harness`, `model`, `stack`, `stack_hash`, `status`, `exit_code`, `error`,
`result_text`, `structured_output`, `cost_usd`, `cost_source`, `usage`, `num_turns`, `duration_ms`, `session_id`,
`resumable`, `resumed_from`, `run_dir`, `home_dir`, `cwd`, `project_config`, `autonomy`, `unsafe`,
`credential_kind`, `started_at`, `finished_at`, `cli_version`, `sandbox`, `warnings`, `raw_final`,
`events_path`, `stderr_path`, `session_cost_usd`); field table in [Results and events](../concepts/results-and-events.md#runresult).

| Member | Purpose |
|---|---|
| `.to_dict() -> dict` | JSON-ready dict (the `result.json` format) |
| `RunResult.from_dict(data) -> RunResult` | rebuild from `to_dict()` output |

### RunStatus

`StrEnum`: `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `BUDGET_EXCEEDED`, `CANCELLED`, `AUTH_FAILED` (values are the
lower-case strings). Derived from `RunResult.error.kind`.

### RunError

`RunError(kind, message)`: why a run did not succeed. Kinds: `cancelled`, `timeout`, `idle`, `schema`, `budget`, `stream_overflow`,
`phd_internal`, `session`, `auth`, `model_unavailable`, `billing`, `harness_error`, `harness_crash`, `no_result`.

### Usage

`Usage(input_tokens=0, cached_input_tokens=0, output_tokens=0, reasoning_tokens=0)`, `.add(other) -> None`.
`input_tokens` includes cached input.

### Event

`Event(run_id, harness, type, data, ts, raw=None, summary="")`, `.to_dict() -> dict`. Frozen. `summary` is a one-line,
secret-scrubbed description (at most 500 characters) of `message`, `tool_call` and `tool_result` events and `""`
for the rest; see [Event summary](../concepts/results-and-events.md#event-summary). `type` is one of
`run.started`, `session.started`, `message`, `tool_call`, `tool_result`, `usage`, `log`, `schema.repair`, `run.finished`.

### LocalLedger

| Member | Purpose |
|---|---|
| `LocalLedger(root=None)` | ledger at `root` (default `~/.phd/runs`) |
| `.root` | absolute ledger directory |
| `.list(*, harness=None, stack=None, since=None) -> list[RunResult]` | finished runs, oldest first |
| `.get(run_id) -> RunResult` | one run; `KeyError` if no `result.json` |
| `await .put(result) -> None` | write `result.json` atomically (it is a `ResultSink`) |
| `.allocate() -> tuple[str, Path]` | create a new run directory (used by `Dispatcher`) |
| `.write_record(run_dir, record) -> None` / `.read_record(run_id) -> dict` | `run.json` access (used by `Dispatcher`) |

### ResultSink

`Protocol` with one method, `async def put(self, result: RunResult) -> None`. Pass instances via
`Dispatcher(sinks=[...])`.

### phd.runs

| Function | Purpose |
|---|---|
| `phd.runs.list(**filters) -> list[RunResult]` | `LocalLedger().list(**filters)` on the default ledger |
| `phd.runs.get(run_id) -> RunResult` | `LocalLedger().get(run_id)` on the default ledger |

## Full reference

Rendered from the docstrings (MkDocs site only).

::: phd.RunResult

::: phd.RunStatus

::: phd.RunError

::: phd.Usage

::: phd.Event

::: phd.LocalLedger

::: phd.ResultSink

::: phd.runs

---

**Next:** [Models](models.md) · **See also:** [API overview](index.md), [Results and events](../concepts/results-and-events.md)
