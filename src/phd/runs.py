"""Query the default run ledger (`~/.phd/runs`): `phd.runs.list(...)` and `phd.runs.get(run_id)`.

Runs recorded under a custom `runs_root` are not visible here; use `LocalLedger(root)` instead.
The implementation lives in `phd.results.runs`."""

from .results.runs import get, list  # noqa: A004 - mirrors CoreDesign's phd.runs.list

__all__ = ["get", "list"]
