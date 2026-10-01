"""Dispatcher: validate → materialize → launch (see RunHandle for collect → ledger)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .._util.fs import is_within
from ..constants import REDACTED
from ..errors import InvalidRunsRoot, NotResumable
from ..harnesses import get_adapter
from ..harnesses.catalog import ModelCatalog
from ..materialize import materialize
from ..materialize.context import MaterializedHome
from ..materialize.prompt import resume_message
from ..results import LocalLedger, ResultSink
from ..results.model import RunResult
from ..spec import Inputs, Spec
from .handle import RunHandle


class Dispatcher:
    def __init__(self, spec: Spec, *, runs_root: str | Path | None = None, sinks: Sequence[ResultSink] = (),
                 catalog: ModelCatalog | None = None, validate_model: bool = True,
                 executables: dict[str, list[str]] | None = None, allow_temp_runs_root: bool = False):
        self.spec = spec
        self.adapter = get_adapter(spec.harness)
        self.ledger = LocalLedger(runs_root)
        self.sinks: list[ResultSink] = [self.ledger, *sinks]
        self.executables = dict(executables or {})
        self.warnings: list[str] = list(self.adapter.validate(spec))
        if validate_model:
            self.warnings += (catalog or ModelCatalog()).validate(spec.harness, spec.model)
        temp = Path(tempfile.gettempdir())
        if spec.harness == "codex" and not allow_temp_runs_root and is_within(self.ledger.root, temp):
            raise InvalidRunsRoot(f"codex refuses a CODEX_HOME under the system temp dir; choose a runs_root outside {temp}")

    # ---- fresh runs
    def plan(self, inputs: Inputs) -> MaterializedHome:
        return self._plan(inputs)[0]

    def _plan(self, inputs: Inputs) -> tuple[MaterializedHome, str]:
        stack_hash = self.spec.stack.content_hash()  # pinned at plan time: what actually runs
        run_id, run_dir = self.ledger.allocate()
        m = materialize(self.spec, inputs, run_id=run_id, run_dir=run_dir)
        m.warnings[:0] = self.warnings
        self.ledger.write_record(run_dir, self._record(m, base_argv=m.argv, resumed_from=None))
        return m, stack_hash

    async def start(self, inputs: Inputs) -> RunHandle:
        m, stack_hash = self._plan(inputs)
        return await RunHandle.launch(self, m, resumed_from=None, stack_hash=stack_hash)

    def spawn(self, inputs: Inputs) -> RunResult:
        return _run_sync(self._spawn(inputs))

    async def _spawn(self, inputs: Inputs) -> RunResult:
        return await (await self.start(inputs)).wait()

    # ---- resume
    async def start_resume(self, previous: RunResult | str, msg: str) -> RunHandle:
        if isinstance(previous, RunResult):
            prev = previous
        else:
            try:
                prev = self.ledger.get(previous)
            except KeyError:
                raise NotResumable(f"no finished run {previous!r} in {self.ledger.root}") from None
        if prev.harness != self.spec.harness:
            raise NotResumable(f"run {prev.run_id} used {prev.harness!r}; this dispatcher runs {self.spec.harness!r}")
        if not prev.session_id:
            raise NotResumable(f"run {prev.run_id} has no session id (status {prev.status})")
        home = Path(prev.home_dir)
        if not home.is_dir():
            raise NotResumable(f"run home {home} no longer exists (resumable=False or cleaned up)")
        record = self.ledger.read_record(prev.run_id)
        stdin = resume_message(msg)
        run_id, run_dir = self.ledger.allocate()
        env = {k: v for k, v in record["env"].items() if v != REDACTED} | self.adapter.credential_env(self.spec, home)
        argv = self.adapter.resume_argv(record["base_argv"], prev.session_id, self.spec)
        m = MaterializedHome(run_id=run_id, run_dir=run_dir, home=home, argv=argv, env=env,
                             stdin=stdin, warnings=list(self.warnings))
        self.ledger.write_record(run_dir, self._record(m, base_argv=record["base_argv"], resumed_from=prev.run_id))
        return await RunHandle.launch(self, m, resumed_from=prev.run_id, stack_hash=prev.stack_hash)

    def resume(self, previous: RunResult | str, msg: str) -> RunResult:
        return _run_sync(self._resume(previous, msg))

    async def _resume(self, previous: RunResult | str, msg: str) -> RunResult:
        return await (await self.start_resume(previous, msg)).wait()

    def _record(self, m: MaterializedHome, *, base_argv: list[str], resumed_from: str | None) -> dict:
        return {"run_id": m.run_id, "harness": self.spec.harness, "model": self.spec.model,
                "cwd": str(self.spec.cwd), "argv": m.argv, "base_argv": base_argv, "env": m.redacted_env(),
                "stdin": m.stdin, "warnings": m.warnings, "resumed_from": resumed_from, "home": str(m.home)}


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("spawn()/resume() cannot run inside an event loop; use `await dispatcher.start(...)` "
                       "or `await dispatcher.start_resume(...)` instead")
