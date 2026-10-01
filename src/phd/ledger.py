"""Run ledger (CoreDesign §11): ~/.phd/runs/<run_id>/{home/, run.json, result.json,
events.jsonl, stderr.log}. ResultSink is the seam for pushing results elsewhere later."""

import builtins
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ._util.fs import write_json
from .errors import NotResumable
from .result import RunResult

DEFAULT_RUNS_ROOT = Path.home() / ".phd" / "runs"


class ResultSink(Protocol):
    async def put(self, result: RunResult) -> None: ...


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


class LocalLedger:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or DEFAULT_RUNS_ROOT).expanduser().resolve()

    def allocate(self) -> tuple[str, Path]:
        while True:
            run_id = new_run_id()
            run_dir = self.root / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                return run_id, run_dir
            except FileExistsError:
                continue

    def write_record(self, run_dir: Path, record: dict) -> None:
        write_json(run_dir / "run.json", record)

    def read_record(self, run_id: str) -> dict:
        path = self.root / run_id / "run.json"
        if not path.is_file():
            raise NotResumable(f"no run record for {run_id!r} in {self.root}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def put(self, result: RunResult) -> None:
        path = Path(result.run_dir) / "result.json"
        tmp = path.with_name("result.json.tmp")
        write_json(tmp, result.to_dict())
        os.replace(tmp, path)

    def get(self, run_id: str) -> RunResult:
        path = self.root / run_id / "result.json"
        if not path.is_file():
            raise KeyError(f"no result for run {run_id!r} in {self.root}")
        return RunResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self, *, harness: str | None = None, stack: str | None = None,
             since: datetime | None = None) -> builtins.list[RunResult]:
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
