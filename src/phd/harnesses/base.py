"""The adapter and stream-parser protocols every harness implements."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..spec import Spec

if TYPE_CHECKING:
    from ..materialize import MaterializeContext
    from .outcome import Outcome


class StreamParser(Protocol):
    def feed(self, obj: dict) -> list[tuple[str, dict]]: ...
    def summarize(self, kind: str, data: dict) -> str: ...
    def running_cost(self) -> float | None: ...
    def outcome(self) -> Outcome: ...


class Adapter(Protocol):
    """One harness. Besides the methods:

    Attributes:
        name: The harness name.
        cumulative_cost: `True` when the harness's reported cost is the whole session's so far rather than
            this invocation's (claude on `--resume`); PHD then subtracts the session's earlier cost to get
            the run's own `cost_usd` and budget figure, and records the reported total as
            `RunResult.session_cost_usd`.
    """

    name: str
    cumulative_cost: bool

    def validate(self, spec: Spec) -> list[str]: ...
    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]: ...
    def owned_paths(self, home: Path) -> list[Path]: ...
    def secret_paths(self, home: Path) -> list[Path]:
        """Credential files `materialize` copies into `home` (a subset of `owned_paths`). PHD deletes them when the
        turn ends, however it ends, and after a dry run; `[]` when credentials only go in the environment."""
        ...
    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]: ...
    def resume_argv(self, fresh_argv: list[str], session_id: str, spec: Spec) -> list[str]: ...
    def restore_answer(self, spec: Spec, value: Any) -> Any: ...
    def parser(self) -> StreamParser: ...
