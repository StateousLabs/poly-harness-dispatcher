"""RunHandle: one live run — pump the harness stream into events, build the RunResult, feed the sinks."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TextIO

from .._util.fs import robust_rmtree
from .._util.shell import resolve_argv
from ..constants import REDACTED
from ..harnesses.outcome import Outcome
from ..materialize.context import MaterializedHome
from ..results.model import Event, RunError, RunResult, Usage, decide_status
from .process import HarnessProcess, child_env

if TYPE_CHECKING:
    from .dispatcher import Dispatcher


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
