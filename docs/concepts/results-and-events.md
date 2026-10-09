# Results and events

This page documents every `RunResult` field, `RunStatus`, the error kinds and their precedence, the event types
and their payloads per harness, `cost_source`, and the ledger, `ResultSink` and `phd.runs`.

Rule: **errors before launch are raised** (`PhdError`); **outcomes after launch are data**. Every launched run
produces a `RunResult`, and `spawn()` and `RunHandle.wait()` do not raise for a failed run. The one
exception is an interrupted pump task (see [Run lifecycle](run-lifecycle.md#how-a-run-ends)).

## RunResult

| Field | Type | Meaning |
|---|---|---|
| `run_id` | `str` | `YYYYmmdd-HHMMSS-<6 hex>` |
| `harness` | `str` | `claude`, `codex`, `opencode` |
| `model` | `str` | `Spec.model` of the dispatcher that ran it |
| `stack` | `str` | `"<name>@<version>"` |
| `stack_hash` | `str` | `Stack.content_hash()` of this run's `Spec.stack`, taken at plan time (also on a resumed turn, whose home is re-materialized) |
| `status` | `RunStatus` | derived from `error` ([below](#runstatus)) |
| `exit_code` | `int` or `None` | harness exit code (`None` only if PHD failed before it was known) |
| `error` | `RunError` or `None` | `kind` + `message` ([below](#error-kinds-and-precedence)) |
| `result_text` | `str` or `None` | final answer: claude `result`; codex last completed `agent_message`; opencode last `text` part |
| `structured_output` | any | claude's `structured_output`; otherwise, when `Spec.output_schema` is set, `result_text` parsed as JSON (a single fenced block is accepted). Kept even when it fails validation; `None` if no JSON could be extracted |
| `cost_usd` | `float` or `None` | this run's own cost, as reported by the harness. claude reports a session-cumulative `total_cost_usd` on `--resume`, so for a resumed claude turn (and a repair turn) PHD subtracts what the session had cost before this invocation; with a repair turn, the sum of both turns' own costs. `budget_usd` is checked against this figure |
| `cost_source` | `str` | [below](#cost_source) |
| `usage` | `Usage` | `input_tokens` (includes cached), `cached_input_tokens`, `output_tokens`, `reasoning_tokens` |
| `num_turns` | `int` or `None` | claude: reported; codex: count of `turn.completed`; opencode: count of `step_finish` |
| `duration_ms` | `int` | from launch to result (monotonic clock) |
| `session_id` | `str` or `None` | claude `session_id`; codex `thread_id`; opencode `sessionID` (also available live as `RunHandle.session_id`, see `session.started`) |
| `resumable` | `bool` | a session id is known and the home is kept: `Spec.resumable` for a fresh run (`False` after the home was deleted); always kept on a resumed turn |
| `resumed_from` | `str` or `None` | for a turn started with `session_id=`, the run the session was found in (the latest run of that session) |
| `run_dir` | `str` | `<runs_root>/<run_id>` |
| `home_dir` | `str` | the run home (for a resumed turn, the session's shared home) |
| `cwd` | `str` | absolute working directory |
| `project_config`, `autonomy`, `unsafe` | `str`, `str`, `bool` | copied from the `Spec` |
| `credential_kind` | `str` | `Credentials.kind`; never the secret |
| `started_at`, `finished_at` | `str` | ISO 8601, UTC (`+00:00`) |
| `cli_version` | `str` or `None` | claude only (`claude_code_version` from the init event); `None` for codex and opencode |
| `sandbox` | `None` | reserved for the sandbox layer; always `None` today |
| `warnings` | `list[str]` | pre-launch warnings, `harness_config` conflicts, `orphan protection unavailable: ...`, `schema repair turn used`, `schema repair turn could not start: ...`, `schema repair turn failed: ...`, `schema repair skipped: ...`, home-cleanup and sink failures |
| `raw_final` | `dict` or `None` | claude `result` line; last codex `turn.completed`; last opencode `step_finish` |
| `events_path` | `str` | `<run_dir>/events.jsonl` |
| `stderr_path` | `str` | `<run_dir>/stderr.log` |
| `session_cost_usd` | `float` or `None` | claude only: the harness's own cumulative cost of the whole session after this run (the last `total_cost_usd` it reported); `None` for codex and opencode (their cost is per invocation), when claude reported no cost and the session had none before, and in `result.json` files written before this field existed. A turn that reports no cost (cancelled, timed out or crashed before claude's `result` line) carries the previous figure forward, so the next resume is not charged the whole session. If claude reports less than the baseline, that figure is taken as the turn's own cost |

`RunResult.to_dict()` / `RunResult.from_dict()` convert to and from the `result.json` form.

## RunStatus

`RunStatus` is a `StrEnum` (`RunStatus.SUCCEEDED == "succeeded"`).

| Status | When |
|---|---|
| `succeeded` | `error is None` |
| `cancelled` | `error.kind == "cancelled"` |
| `timed_out` | `error.kind` is `timeout` or `idle` |
| `budget_exceeded` | `error.kind == "budget"` |
| `auth_failed` | `error.kind == "auth"` |
| `failed` | any other error kind |

## Error kinds and precedence

PHD picks **one** error, checking these in order; the first that applies wins:

| Rank | `kind` | Condition | Message |
|---|---|---|---|
| 1 | `cancelled` | `RunHandle.cancel()` was called | `run was cancelled` |
| 2 | `timeout` | `timeout_s` elapsed | `no result within timeout_s=...` |
| 3 | `idle` | no stdout line for `idle_timeout_s` | `no output for idle_timeout_s=<n>` |
| 4 | `budget` | PHD stopped the run on streamed cost | `cost ... exceeded budget_usd=...` |
| 5 | `stream_overflow` | a stdout line exceeded 16 MiB | |
| 6 | `phd_internal` | an exception inside PHD after launch | exception type and text |
| 7 | `session` or parser error | the `session_id` cannot be resumed (reported before launch), or the harness reported that the session no longer exists; otherwise the harness reported an error in its stream (classified below) | what cannot be resumed, or the harness's message |
| 8 | `harness_crash` | exit code is not 0 | `exit code N; stderr: <last 2000 chars, secrets scrubbed>` |
| 9 | `no_result` | exit code 0 but no final text and no final event | `harness exited without a final result` |
| 10 | `schema` | the run was otherwise fine but `Spec.output_schema` is set and the answer is not JSON or does not match | `final answer is not JSON`, or `<json path>: <message>` (scrubbed of secrets, capped at 500 characters); after a [repair turn](run-lifecycle.md#schema-repair-turn) prefixed `after one repair turn: ` |

!!! note "Stable contract"
    The error kinds (`phd.ERROR_KINDS`, listed in precedence order) and the statuses are a stable public contract:
    new kinds may be added, existing ones are never renamed or removed. Branch on `error.kind`, never on
    `error.message`. `session` maps to status `failed`.

A run is `succeeded` only when none of these apply. A claude run whose final reported cost exceeds `budget_usd`
gets `budget` (rank 4) even if claude itself finished normally.

**Parser error classification.** claude: the `result` line has `is_error` or a `subtype` starting with `error`
(subtypes containing `budget` give `budget`). codex: a `turn.failed` or `error` event (a later
`turn.completed` clears it). opencode: a non-retryable `error` event. The message is classified by
case-insensitive substring, first match wins:

| `kind` | Message contains |
|---|---|
| `model_unavailable` | `do not have access`, or `model` together with `does not exist` / `not found` / `not supported` |
| `billing` | `insufficient` with `fund` or `credit`; the word `402`; `billing` |
| `auth` | `not logged in`, `unauthorized`, the word `401`, `authentication`, `invalid api key` |
| `budget` | `budget` |
| `harness_error` | anything else |

The model catalogue proves only that a model exists; a model the account may not use surfaces here as
`model_unavailable`.

## Events

`RunHandle.events()` yields `Event` objects, and each one is also appended to `events.jsonl`:

```json
{"run_id": "...", "harness": "claude", "type": "tool_call", "data": {"tool": "Bash", "input": {...}, "id": "toolu_..."}, "ts": 1780000000.0, "raw": {...}}
```

`ts` is PHD's emit time (Unix seconds). `raw` is the harness JSON line the event came from, or `None` for PHD's
own events.

| `type` | Emitted by | `data` |
|---|---|---|
| `run.started` | PHD, first | `{"argv": [...], "resumed_from": ...}` |
| `session.started` | PHD | `{"session_id": ...}`: once per run, when the harness first reports its session id (a resumed turn: right after `run.started`) |
| `message` | parser | `{"text": ...}` |
| `tool_call` | parser | claude `{"tool", "input", "id"}`; codex and opencode `{"tool", "id", "status"}` |
| `tool_result` | parser | claude `{"id", "is_error"}`; codex `{"tool", "id", "status"}` |
| `usage` | parser | `Usage` fields so far + `"cost_usd"` |
| `schema.repair` | PHD | `{"error": ...}`: the schema error that triggered the [repair turn](run-lifecycle.md#schema-repair-turn); emitted when the repair turn starts (after its process started) |
| `log` | PHD or parser | non-JSON stdout line: `{"text": line}`; parser warning: `{"text", "level": "warning"}`; unparsable event: `{"text": "unparsed <harness> event", "raw_type"}` |
| `run.finished` | PHD, last | `{"status": "<status>"}` |

JSON stdout lines that are not objects are ignored. Tool names in events are the harness's native names (codex
uses item types, see below), not neutral names.

Per harness, which native stream items produce which events:

| Native item | claude | codex | opencode |
|---|---|---|---|
| session start | `system` / `init`: sets `session_id`, `cli_version`; PHD emits `session.started` | `thread.started`: sets `session_id`; PHD emits `session.started` | `sessionID` on any event; PHD emits `session.started` on the first one |
| assistant text | `assistant` text item → `message` | `item.completed` `agent_message` → `message` | `text` → `message` |
| tool call | `assistant` `tool_use` item → `tool_call` | `item.started` of `command_execution`, `mcp_tool_call` (tool `mcp:<server>/<tool>`), `file_change`, `web_search` → `tool_call` | `tool_use` → `tool_call` |
| tool result | `user` `tool_result` item → `tool_result` | `item.completed` of those types → `tool_result` | none |
| usage / cost | `result` → `usage` (once, with `cost_usd` as claude reported it: session-cumulative on a resumed turn) | `turn.completed` → `usage` (cumulative, `cost_usd: null`) | `step_finish` → `usage` (cumulative tokens and cost) |
| warnings | none | `item.completed` `error` item → `log` warning (except the hook-trust warning) | retryable `error` → `log` warning |
| failure | `result` with `is_error` → error, no event | `turn.failed` / `error` → error, no event | non-retryable `error` → error, no event |

`events()` reads one queue: each event goes to exactly one iterator, so use one consumer per handle. Iterating
again after the run finished yields nothing; read `events.jsonl` instead.

## cost_source

| Value | Meaning |
|---|---|
| `harness` | the cost came from the harness: claude when the `result` line has `total_cost_usd`; opencode always (sum of `step_finish.cost`; `cost_usd` is `None` if no step finished, `0` for free models) |
| `unknown` | no cost: always for codex; claude without `total_cost_usd`; PHD-internal failures; a run with a schema repair turn in which only one turn reported a cost |
| `estimated` | reserved for a price table; never produced today |

## Ledger, ResultSink, phd.runs

Every `Dispatcher` has a `LocalLedger` at `runs_root` (default `~/.phd/runs`) as its first sink.

```python
import phd
from datetime import datetime, timedelta, timezone
from phd import LocalLedger

recent = phd.runs.list(harness="claude", since=datetime.now(timezone.utc) - timedelta(days=1))
one = phd.runs.get(recent[-1].run_id)

custom = LocalLedger("D:/phd-runs")       # same API for a custom runs_root
custom.list(stack="fixture-helper@0.1.0")
```

| API | Behaviour |
|---|---|
| `LocalLedger(root=None)` | ledger at `root` or `~/.phd/runs`; nothing is created until a run is allocated |
| `.list(*, harness=None, stack=None, since=None)` | every `*/result.json` that parses, filtered, sorted by `started_at`; corrupt files are skipped. `since` must be timezone-aware (comparing with a naive datetime raises `TypeError`) |
| `.get(run_id)` | one `RunResult`; `KeyError` if there is no `result.json` (unknown id, dry run, still running) |
| `.put(result)` | writes `result.json` atomically (temp file + `os.replace`) |
| `phd.runs.list(**filters)`, `phd.runs.get(run_id)` | the same on the default ledger only |

**Custom sinks.** Any object with `async def put(self, result: RunResult) -> None` is a `ResultSink`:

```python
class PrintSink:
    async def put(self, result):
        print(result.run_id, result.status, result.cost_usd)

Dispatcher(spec, sinks=[PrintSink()])
```

Sinks run in order: the ledger first, then yours. An exception in a sink is caught and appended to
`result.warnings`; later sinks still run and the result is still returned. Because the ledger has already written
`result.json` by then, a warning about a failing extra sink appears in the returned `RunResult` (and in later
sinks) but not in `result.json`.

---

**Next:** [Option combinations](combinations.md) · **See also:** [Run lifecycle](run-lifecycle.md), [API: Results](../reference/results.md), [Troubleshooting](../troubleshooting.md)
