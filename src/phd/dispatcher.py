"""Dispatcher and RunHandle: validate → materialize → launch → collect → ledger."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from ._util.fs import is_within, robust_rmtree
from ._util.shell import resolve_argv
from .harnesses import get_adapter
from .harnesses.catalog import ModelCatalog
from .harnesses.outcome import Outcome
from .materialize.context import MaterializedHome
from .materialize.prompt import resume_message
from .constants import REDACTED
from .errors import InvalidRunsRoot, NotResumable
from .launcher import HarnessProcess, child_env
from .results import LocalLedger, ResultSink
from .materialize import materialize
from .results.model import Event, RunError, RunResult, Usage, decide_status
from .spec import Inputs, Spec



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


class RunHandle:
    def __init__(self, dispatcher: Dispatcher, m: MaterializedHome, resumed_from: str | None, stack_hash: str):
        self._d = dispatcher
        self._stack_hash = stack_hash
        self.materialized = m
        self.run_id = m.run_id
        self.resumed_from = resumed_from
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._result: RunResult | None = None
        self._cancelled = False
        self._proc: HarnessProcess | None = None
        self._task: asyncio.Task | None = None
        self._started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()

    @classmethod
    async def launch(cls, d: Dispatcher, m: MaterializedHome, *, resumed_from: str | None,
                     stack_hash: str) -> RunHandle:
        handle = cls(d, m, resumed_from, stack_hash)
        override = d.executables.get(d.spec.harness)
        argv = [*override, *m.argv[1:]] if override else resolve_argv(m.argv)
        handle._proc = HarnessProcess(argv, env=child_env(m.env), cwd=d.spec.cwd, stdin_text=m.stdin,
                                      stderr_path=m.run_dir / "stderr.log")
        await handle._proc.start()
        handle._task = asyncio.create_task(handle._pump())
        return handle

    async def events(self) -> AsyncIterator[Event]:
        while (event := await self._queue.get()) is not None:
            yield event
        self._queue.put_nowait(None)  # leave the sentinel for any other / later iterator

    async def wait(self) -> RunResult:
        await asyncio.shield(self._task)
        return self._result

    async def cancel(self) -> RunResult:
        self._cancelled = True
        self._proc.kill_tree()
        return await self.wait()

    def _emit(self, sink: TextIO, kind: str, data: dict, raw: dict | None = None) -> None:
        event = Event(run_id=self.run_id, harness=self._d.spec.harness, type=kind, data=data, ts=time.time(), raw=raw)
        sink.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        self._queue.put_nowait(event)

    async def _pump(self) -> None:
        result: RunResult | None = None
        try:
            try:
                result = await self._run_to_result()
            except Exception as e:  # never let wait() raise or events() hang (CoreDesign §3)
                self._kill_if_running()
                result = self._minimal_result(e)
            except BaseException:  # KeyboardInterrupt / CancelledError: never leave the harness running
                self._kill_if_running()
                raise
            for sink in self._d.sinks:
                try:
                    await sink.put(result)
                except Exception as e:
                    result.warnings.append(f"result sink {type(sink).__name__} failed: {e}")
        finally:
            self._result = result
            self._queue.put_nowait(None)

    def _kill_if_running(self) -> None:
        proc = self._proc
        if proc is not None and proc.proc is not None and proc.proc.returncode is None:
            proc.kill_tree()

    def _minimal_result(self, e: Exception) -> RunResult:
        spec, m = self._d.spec, self.materialized
        return RunResult(
            run_id=m.run_id, harness=spec.harness, model=spec.model, stack=spec.stack.ref,
            stack_hash=self._stack_hash, status=decide_status(RunError("phd_internal", "")), exit_code=None,
            error=RunError("phd_internal", f"{type(e).__name__}: {e}"), result_text=None, structured_output=None,
            cost_usd=None, cost_source="unknown", usage=Usage(), num_turns=None,
            duration_ms=int((time.monotonic() - self._t0) * 1000), session_id=None, resumable=False,
            resumed_from=self.resumed_from, run_dir=str(m.run_dir), home_dir=str(m.home), cwd=str(spec.cwd),
            project_config=spec.project_config, autonomy=spec.autonomy, unsafe=spec.unsafe,
            credential_kind=spec.credentials.kind, started_at=self._started_at.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(), warnings=list(m.warnings),
            events_path=str(m.run_dir / "events.jsonl"), stderr_path=str(m.run_dir / "stderr.log"))

    async def _run_to_result(self) -> RunResult:
        spec, m = self._d.spec, self.materialized
        parser = self._d.adapter.parser()
        timed_out = budget_hit = False
        exit_code: int | None = None
        internal: Exception | None = None
        with open(m.run_dir / "events.jsonl", "a", encoding="utf-8") as events_file:
            self._emit(events_file, "run.started", {"argv": m.argv, "resumed_from": self.resumed_from})
            try:
                async with asyncio.timeout(spec.timeout_s):
                    budget_hit = await self._consume(events_file, parser)
                    exit_code = await self._proc.wait()
            except TimeoutError:
                timed_out = True
                self._proc.kill_tree()
                exit_code = await self._proc.wait()
            except Exception as e:  # post-launch problems are data, never raised (CoreDesign §3)
                internal = e
                self._proc.kill_tree()
                exit_code = await self._proc.wait()
            try:
                result = self._build_result(parser.outcome(), exit_code, timed_out=timed_out,
                                            budget_hit=budget_hit, internal=internal)
            except Exception as e:
                result = self._minimal_result(e)
                result.exit_code = exit_code
            self._emit(events_file, "run.finished", {"status": str(result.status)})
        if not spec.resumable and self.resumed_from is None:
            try:
                robust_rmtree(m.home)
            except OSError as e:
                result.warnings.append(f"could not remove run home {m.home}: {e}")
            result.resumable = False
        return result

    async def _consume(self, events_file: TextIO, parser) -> bool:
        budget = self._d.spec.budget_usd
        async for line in self._proc.lines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self._emit(events_file, "log", {"text": line})
                continue
            if not isinstance(obj, dict):
                continue
            for kind, data in parser.feed(obj):
                self._emit(events_file, kind, data, raw=obj)
            cost = parser.running_cost()
            if budget is not None and cost is not None and cost > budget:
                self._proc.kill_tree()
                return True
        return False

    def _scrub(self, text: str) -> str:
        spec = self._d.spec
        for value in [*spec.secrets.values(), spec.credentials.secret]:
            if value:
                text = text.replace(value, REDACTED)
        return text

    def _build_result(self, o: Outcome, exit_code: int | None, *, timed_out: bool, budget_hit: bool,
                      internal: Exception | None) -> RunResult:
        spec, m = self._d.spec, self.materialized
        if self._cancelled:
            error = RunError("cancelled", "run was cancelled")
        elif timed_out:
            error = RunError("timeout", f"no result within timeout_s={spec.timeout_s}")
        elif budget_hit:
            error = RunError("budget", f"cost {o.cost_usd} exceeded budget_usd={spec.budget_usd}")
        elif self._proc.overflowed:
            error = RunError("stream_overflow", "a stdout line exceeded the stream limit; run was stopped")
        elif internal is not None:
            error = RunError("phd_internal", f"{type(internal).__name__}: {internal}")
        elif o.error is not None:
            error = o.error
        elif exit_code != 0:
            error = RunError("harness_crash", f"exit code {exit_code}; stderr: {self._scrub(self._proc.stderr_tail())}")
        elif o.result_text is None and o.raw_final is None:
            error = RunError("no_result", "harness exited without a final result")
        else:
            error = None
        structured = o.structured_output
        if structured is None and spec.output_schema is not None and o.result_text:
            try:
                structured = json.loads(o.result_text)
            except json.JSONDecodeError:
                pass
        return RunResult(
            run_id=m.run_id, harness=spec.harness, model=spec.model, stack=spec.stack.ref,
            stack_hash=self._stack_hash, status=decide_status(error), exit_code=exit_code, error=error,
            result_text=o.result_text, structured_output=structured, cost_usd=o.cost_usd,
            cost_source=o.cost_source, usage=o.usage, num_turns=o.num_turns,
            duration_ms=int((time.monotonic() - self._t0) * 1000), session_id=o.session_id,
            resumable=spec.resumable and o.session_id is not None, resumed_from=self.resumed_from,
            run_dir=str(m.run_dir), home_dir=str(m.home), cwd=str(spec.cwd), project_config=spec.project_config,
            autonomy=spec.autonomy, unsafe=spec.unsafe, credential_kind=spec.credentials.kind,
            started_at=self._started_at.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
            cli_version=o.cli_version, warnings=list(m.warnings), raw_final=o.raw_final,
            events_path=str(m.run_dir / "events.jsonl"), stderr_path=str(m.run_dir / "stderr.log"))
