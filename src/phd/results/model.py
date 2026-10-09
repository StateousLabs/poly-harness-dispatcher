"""RunResult and normalized events (CoreDesign §11)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    """Overall outcome of a run. Derived from `RunResult.error`.

    `SUCCEEDED` when there is no error; `CANCELLED`, `TIMED_OUT`, `BUDGET_EXCEEDED` and
    `AUTH_FAILED` for error kinds `cancelled`, `timeout` / `idle`, `budget` and `auth`; `FAILED` for
    every other error kind. Members compare equal to their string values.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    AUTH_FAILED = "auth_failed"


@dataclass
class Usage:
    """Token counts for a run, normalized across harnesses.

    Attributes:
        input_tokens: All input tokens, *including* cached ones (claude and opencode add cache
            reads and cache writes to the uncached count).
        cached_input_tokens: Input tokens served from cache (cache reads).
        output_tokens: Output tokens.
        reasoning_tokens: Reasoning / thinking tokens where the harness reports them.
    """

    input_tokens: int = 0  # includes cached input
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: Usage) -> None:
        """Add `other`'s counts to this one in place."""
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens


# Every error kind, in the order PHD picks one (first that applies wins). `session` sits with the kinds the
# harness stream reports (it is also raised before launch for a session that cannot be resumed). Stable:
# kinds may be added, never renamed or removed.
ERROR_KINDS: tuple[str, ...] = (
    "cancelled", "timeout", "idle", "budget", "stream_overflow", "phd_internal", "session", "auth", "billing",
    "model_unavailable", "harness_error", "harness_crash", "no_result", "schema")


@dataclass
class RunError:
    """Why a run did not succeed.

    Attributes:
        kind: One of `ERROR_KINDS`: `cancelled`, `timeout`, `idle`, `budget`, `stream_overflow`, `phd_internal`,
            `session`, `auth`, `billing`, `model_unavailable`, `harness_error`, `harness_crash`, `no_result`,
            `schema`. The kinds are a stable contract (new ones may be added, none renamed or removed); read
            `kind`, never `message`. See `docs/concepts/results-and-events.md` for precedence.
        message: Human-readable detail (harness message, or exit code plus scrubbed stderr tail).
    """

    kind: str  # one of ERROR_KINDS
    message: str


_STATUS_BY_KIND = {"cancelled": RunStatus.CANCELLED, "timeout": RunStatus.TIMED_OUT,
                   "idle": RunStatus.TIMED_OUT, "budget": RunStatus.BUDGET_EXCEEDED,
                   "auth": RunStatus.AUTH_FAILED}


def decide_status(error: RunError | None) -> RunStatus:
    return RunStatus.SUCCEEDED if error is None else _STATUS_BY_KIND.get(error.kind, RunStatus.FAILED)


@dataclass(frozen=True)
class Event:
    """One normalized event from a run, as yielded by `RunHandle.events` and stored in `events.jsonl`.

    Attributes:
        run_id: The run that produced it.
        harness: The harness name.
        type: `run.started`, `session.started`, `message`, `tool_call`, `tool_result`, `usage`, `log`,
            `schema.repair` or `run.finished`.
        data: Type-specific payload (for example `{"text": ...}` for `message`).
        ts: Unix timestamp (seconds) when PHD emitted the event.
        raw: The harness JSON line the event came from; `None` for PHD's own events
            (`run.started`, `run.finished`, and `log` events for non-JSON stdout lines).
    """

    run_id: str
    harness: str
    type: str  # run.started | session.started | message | tool_call | tool_result | usage | log | schema.repair | run.finished
    data: dict
    ts: float
    raw: dict | None = None

    def to_dict(self) -> dict:
        """Return the event as a JSON-ready dict (the `events.jsonl` line format)."""
        return asdict(self)


@dataclass
class RunResult:
    """The normalized outcome of one run, written to `<run_dir>/result.json`.

    Produced for every launched run, whatever happened; a failure is described by `status` and
    `error`, never raised. Field-by-field reference: `docs/concepts/results-and-events.md`.

    Attributes:
        run_id: `YYYYmmdd-HHMMSS-<6 hex>`.
        harness: Harness name.
        model: `Spec.model`.
        stack: `"<name>@<version>"`.
        stack_hash: `Stack.content_hash()` of this run's stack, taken when the run was planned
            (also for a resumed turn, whose home is re-materialized).
        status: Overall outcome.
        exit_code: Harness exit code, or `None` if unknown.
        error: Why the run did not succeed, or `None`.
        result_text: The agent's final text answer, if any.
        structured_output: The structured answer when `Spec.output_schema` is set (from the
            harness, else parsed from `result_text` as JSON when possible).
        cost_usd: Cost of this run, or `None`. For claude this is the turn's own cost: claude reports a
            session-cumulative figure on `--resume`, so PHD subtracts what the session had cost before
            (see `session_cost_usd`).
        cost_source: `"harness"` (claude when it reported `total_cost_usd`; opencode always)
            or `"unknown"` (codex; claude without a cost; PHD-internal failures). The value
            `"estimated"` is reserved and never produced today.
        usage: Token counts.
        num_turns: Turns reported (claude) or counted (codex `turn.completed`, opencode
            `step_finish`).
        duration_ms: Wall-clock time from launch to result.
        session_id: Harness session/thread id; pass it as `start(..., session_id=)` to resume.
        resumable: `True` when the run home was kept and a session id is known (a resumed
            turn never deletes the shared home).
        resumed_from: For a turn started with `session_id`, the run the session was found in.
        run_dir: The run directory.
        home_dir: The run home (for a resumed turn, the session's shared home).
        cwd: The agent's working directory.
        project_config: `Spec.project_config`.
        autonomy: `Spec.autonomy`.
        unsafe: `Spec.unsafe`.
        credential_kind: `Credentials.kind` (never the secret).
        started_at: ISO 8601 UTC timestamp.
        finished_at: ISO 8601 UTC timestamp.
        cli_version: Harness version (claude only today; `None` for codex and opencode).
        sandbox: Always `None` (no sandbox layer yet).
        warnings: Pre-launch and materialization warnings, plus sink or cleanup failures.
        raw_final: The harness's final JSON object (claude `result` line, last codex
            `turn.completed`, last opencode `step_finish`).
        events_path: Path of `events.jsonl`.
        stderr_path: Path of `stderr.log`.
        session_cost_usd: The harness's own cumulative cost for the whole session after this run, for
            harnesses that report cost per session (claude); `None` otherwise or when not reported.
            Absent from records written before it existed (loads as `None`).
    """

    run_id: str
    harness: str
    model: str
    stack: str
    stack_hash: str
    status: RunStatus
    exit_code: int | None
    error: RunError | None
    result_text: str | None
    structured_output: Any
    cost_usd: float | None
    cost_source: str  # harness | estimated | unknown
    usage: Usage
    num_turns: int | None
    duration_ms: int
    session_id: str | None
    resumable: bool
    resumed_from: str | None
    run_dir: str
    home_dir: str
    cwd: str
    project_config: str
    autonomy: str
    unsafe: bool
    credential_kind: str
    started_at: str
    finished_at: str
    cli_version: str | None = None
    sandbox: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw_final: dict | None = None
    events_path: str = ""
    stderr_path: str = ""
    session_cost_usd: float | None = None

    def to_dict(self) -> dict:
        """Return a JSON-ready dict (the `result.json` format); `status` becomes its string."""
        d = asdict(self)
        d["status"] = str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> RunResult:
        """Rebuild a `RunResult` from `to_dict()` output (for example a parsed `result.json`)."""
        d = dict(data)
        d["status"] = RunStatus(d["status"])
        d["usage"] = Usage(**d["usage"])
        d["error"] = RunError(**d["error"]) if d.get("error") else None
        return cls(**d)
