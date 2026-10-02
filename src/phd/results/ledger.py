"""Run ledger (CoreDesign §11): ~/.phd/runs/<run_id>/{home/, run.json, result.json,
events.jsonl, stderr.log}. ResultSink is the seam for pushing results elsewhere later."""

import builtins
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .._util.fs import write_json
from ..errors import NotResumable
from .model import RunResult

DEFAULT_RUNS_ROOT = Path.home() / ".phd" / "runs"


class ResultSink(Protocol):
    """Anything that receives finished results: implement `async put(result)`.

    Pass instances as `Dispatcher(..., sinks=[...])`. Sinks are called in order after the local
    ledger; an exception from a sink is caught and appended to `result.warnings` (the result is
    still returned, and later sinks still run).

    Example:
        ```python
        class PrintSink:
            async def put(self, result: RunResult) -> None:
                print(result.run_id, result.status)

        Dispatcher(spec, sinks=[PrintSink()])
        ```
    """

    async def put(self, result: RunResult) -> None:
        """Receive one finished `RunResult`."""
        ...


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


class LocalLedger:
    """The on-disk run ledger: one directory per run under `root`.

    Layout per run: `<root>/<run_id>/` with `run.json` (launch record, secrets redacted),
    `result.json`, `events.jsonl`, `stderr.log` and `home/`. It is also the first `ResultSink`
    of every `Dispatcher`.

    Attributes:
        root: Absolute ledger directory.
    """

    def __init__(self, root: str | Path | None = None):
        """Open (lazily) the ledger at `root`; nothing is created until a run is allocated.

        Args:
            root: Ledger directory. Defaults to `~/.phd/runs`.
        """
        self.root = Path(root or DEFAULT_RUNS_ROOT).expanduser().resolve()

    def allocate(self) -> tuple[str, Path]:
        """Create a fresh, uniquely named run directory and return `(run_id, run_dir)`."""
        while True:
            run_id = new_run_id()
            run_dir = self.root / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                return run_id, run_dir
            except FileExistsError:
                continue

    def write_record(self, run_dir: Path, record: dict) -> None:
        """Write the launch record `run.json` into `run_dir`."""
        write_json(run_dir / "run.json", record)

    def read_record(self, run_id: str) -> dict:
        """Read a run's `run.json`.

        Raises:
            NotResumable: The run has no `run.json` in this ledger.
        """
        path = self.root / run_id / "run.json"
        if not path.is_file():
            raise NotResumable(f"no run record for {run_id!r} in {self.root}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def put(self, result: RunResult) -> None:
        """Write `result.json` into `result.run_dir` atomically (temp file + rename)."""
        path = Path(result.run_dir) / "result.json"
        tmp = path.with_name("result.json.tmp")
        write_json(tmp, result.to_dict())
        os.replace(tmp, path)

    def get(self, run_id: str) -> RunResult:
        """Load one run's result.

        Raises:
            KeyError: The run has no `result.json` (unknown id, still running, or a dry run).
        """
        path = self.root / run_id / "result.json"
        if not path.is_file():
            raise KeyError(f"no result for run {run_id!r} in {self.root}")
        return RunResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self, *, harness: str | None = None, stack: str | None = None,
             since: datetime | None = None) -> builtins.list[RunResult]:
        """List finished runs, oldest first (by `started_at`).

        Only runs with a readable `result.json` are listed; corrupt files are skipped.

        Args:
            harness: Keep only runs of this harness.
            stack: Keep only runs of this stack ref (`"name@version"`).
            since: Keep only runs started at or after this time. `started_at` is UTC-aware, so
                pass an aware datetime (comparing with a naive one raises `TypeError`).
        """
        results = []
        for path in self.root.glob("*/result.json"):
            try:
                r = RunResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
                continue  # one corrupt result.json must not hide every other run
            if harness and r.harness != harness:
                continue
            if stack and r.stack != stack:
                continue
            if since and datetime.fromisoformat(r.started_at) < since:
                continue
            results.append(r)
        return sorted(results, key=lambda r: r.started_at)

    def latest_for_session(self, session_id: str) -> RunResult | None:
        """Return the most recently started finished run whose `session_id` matches, or `None`.

        Failed runs count: a run that recorded a session id can be resumed whatever its status.
        """
        matches = [r for r in self.list() if r.session_id == session_id]
        return matches[-1] if matches else None
