"""`import phd.runs` entry point; the implementation lives in `phd.results.runs`."""

from .results.runs import get, list  # noqa: A004 - mirrors CoreDesign's phd.runs.list

__all__ = ["get", "list"]
