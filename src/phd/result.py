"""RunResult and normalized events (CoreDesign §11)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    AUTH_FAILED = "auth_failed"


@dataclass
class Usage:
    input_tokens: int = 0  # includes cached input
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens


@dataclass
class RunError:
    kind: str  # auth | model_unavailable | billing | budget | timeout | cancelled | harness_error | harness_crash | no_result | phd_internal
    message: str


_STATUS_BY_KIND = {"cancelled": RunStatus.CANCELLED, "timeout": RunStatus.TIMED_OUT,
                   "budget": RunStatus.BUDGET_EXCEEDED, "auth": RunStatus.AUTH_FAILED}


def decide_status(error: RunError | None) -> RunStatus:
    return RunStatus.SUCCEEDED if error is None else _STATUS_BY_KIND.get(error.kind, RunStatus.FAILED)


@dataclass(frozen=True)
class Event:
    run_id: str
    harness: str
    type: str  # run.started | message | tool_call | tool_result | usage | log | run.finished
    data: dict
    ts: float
    raw: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> RunResult:
        d = dict(data)
        d["status"] = RunStatus(d["status"])
        d["usage"] = Usage(**d["usage"])
        d["error"] = RunError(**d["error"]) if d.get("error") else None
        return cls(**d)
