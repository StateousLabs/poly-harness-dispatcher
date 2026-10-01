"""`phd.runs.list(...)` / `phd.runs.get(run_id)` over the default ledger."""

import builtins

from .ledger import LocalLedger
from .model import RunResult


def list(**filters) -> builtins.list[RunResult]:  # noqa: A001 - mirrors CoreDesign's phd.runs.list
    return LocalLedger().list(**filters)


def get(run_id: str) -> RunResult:
    return LocalLedger().get(run_id)
