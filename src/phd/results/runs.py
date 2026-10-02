"""`phd.runs.list(...)` / `phd.runs.get(run_id)` over the default ledger."""

import builtins

from .ledger import LocalLedger
from .model import RunResult


def list(**filters) -> builtins.list[RunResult]:  # noqa: A001 - mirrors CoreDesign's phd.runs.list
    """List finished runs in the default ledger (`~/.phd/runs`), oldest first.

    Args:
        **filters: `harness=`, `stack=`, `since=`, as for `LocalLedger.list`.

    Example:
        ```python
        import phd
        for r in phd.runs.list(harness="claude")[-5:]:
            print(r.run_id, r.status, r.cost_usd)
        ```
    """
    return LocalLedger().list(**filters)


def get(run_id: str) -> RunResult:
    """Load one run's result from the default ledger (`~/.phd/runs`).

    Raises:
        KeyError: The run has no `result.json` there.
    """
    return LocalLedger().get(run_id)
