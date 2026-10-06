"""RunHandle: one live run — pump the harness stream into events, build the RunResult, feed the sinks."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TextIO

from .._util.fs import robust_rmtree
from .._util.shell import resolve_argv
from ..constants import REDACTED
from ..harnesses import Outcome
from ..materialize import MaterializedHome
from ..results import Event, RunError, RunResult, Usage
from ..results.model import decide_status
from .process import HarnessProcess, child_env

if TYPE_CHECKING:
    from .dispatcher import Dispatcher
    from .session_lock import SessionLock


@dataclass
class _Turn:
    """What happened to one harness process (a run has one, or two with a schema repair turn)."""

    exit_code: int | None = None
    timed_out: bool = False
    budget_hit: bool = False
    idle_hit: bool = False
    internal: Exception | None = None


class RunHandle:
    """One live run. Created by `Dispatcher.start`, not directly.

    A background task reads the harness's stdout, turns each line into `Event`s (also appended to
    `events.jsonl`), builds the `RunResult` when the process exits, and hands it to every sink.

    Attributes:
        run_id: This run's id.
        resumed_from: For a resumed session, the run the session was found in; else `None`.
        materialized: The `MaterializedHome` that was launched.
        session_id: The harness session id once the harness reports it (at once for a resumed turn); `None` before.
    """

    def __init__(self, dispatcher: Dispatcher, m: MaterializedHome, resumed_from: str | None, stack_hash: str,
                 lock: SessionLock | None = None, session_id: str | None = None):
        self._d = dispatcher
        self._stack_hash = stack_hash
        self._lock = lock  # held for the whole run; released on every exit path
        self.materialized = m
        self.run_id = m.run_id
        self.resumed_from = resumed_from
        self.session_id: str | None = session_id  # a resumed turn knows its session before launch
        self._resume_session = session_id
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._result: RunResult | None = None
        self._cancelled = False
        self._proc: HarnessProcess | None = None
        self._task: asyncio.Task | None = None
        self._started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()

    @classmethod
    async def launch(cls, d: Dispatcher, m: MaterializedHome, *, resumed_from: str | None,
                     stack_hash: str, lock: SessionLock | None = None,
                     session_id: str | None = None) -> RunHandle:
        handle = cls(d, m, resumed_from, stack_hash, lock, session_id)
        handle._proc = HarnessProcess(handle._resolve(m.argv), env=child_env(m.env), cwd=d.spec.cwd, stdin_text=m.stdin,
                                      stderr_path=m.run_dir / "stderr.log")
        await handle._proc.start()
        handle._task = asyncio.create_task(handle._pump())
        return handle

    def _resolve(self, argv: list[str]) -> list[str]:
        override = self._d.executables.get(self._d.spec.harness)
        return [*override, *argv[1:]] if override else resolve_argv(argv)

    @property
    def done(self) -> bool:
        """True once the run has finished and its `RunResult` exists."""
        return self._task is not None and self._task.done()

    def _request_cancel(self) -> None:
        self._cancelled = True
        if self._proc is not None:
            self._proc.kill_tree()

    def _note_session(self, sink: TextIO, session_id: str | None) -> None:
        if session_id and self.session_id is None:
            self.session_id = session_id
            self._emit(sink, "session.started", {"session_id": session_id})

    async def events(self) -> AsyncIterator[Event]:
        """Yield normalized events until the run finishes (the last one is `run.finished`).

        Events go through a single queue: each event is delivered to exactly one iterator, so
        use one consumer. Iterating again after the run has finished yields nothing; read
        `events.jsonl` (`RunResult.events_path`) for the full record.
        """
        while (event := await self._queue.get()) is not None:
            yield event
        self._queue.put_nowait(None)  # leave the sentinel for any other / later iterator

    async def wait(self) -> RunResult:
        """Wait for the run to finish and return its `RunResult`. Never raises for a failed run.

        Cancelling the awaiting task does not stop the run (the wait is shielded); use `cancel`.
        """
        await asyncio.shield(self._task)
        return self._result

    async def cancel(self) -> RunResult:
        """Kill the harness process tree and return the result.

        A run still in progress finishes with status `cancelled`. On a run that has already
        finished this just returns the existing result.
        """
        self._request_cancel()
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
            if self._proc is not None:
                self._proc.close()
            self._release_lock()
            self._result = result
            self._queue.put_nowait(None)

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()

    def _kill_if_running(self) -> None:
        proc = self._proc
        if proc is not None and proc.proc is not None and proc.proc.returncode is None:
            proc.kill_tree()

    def _guard_warnings(self) -> list[str]:
        guard = self._proc.guard if self._proc is not None else None
        return [guard.warning] if guard is not None and guard.warning else []

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
            finished_at=datetime.now(timezone.utc).isoformat(), warnings=[*m.warnings, *self._guard_warnings()],
            events_path=str(m.run_dir / "events.jsonl"), stderr_path=str(m.run_dir / "stderr.log"))

    async def _run_to_result(self) -> RunResult:
        spec, m = self._d.spec, self.materialized
        deadline = asyncio.get_running_loop().time() + spec.timeout_s
        with open(m.run_dir / "events.jsonl", "a", encoding="utf-8") as events_file:
            self._emit(events_file, "run.started", {"argv": m.argv, "resumed_from": self.resumed_from})
            if self._resume_session:
                self._emit(events_file, "session.started", {"session_id": self._resume_session})
            parser = self._d.adapter.parser()
            turn = await self._turn(events_file, parser, deadline)
            result = self._finish(parser.outcome(), turn)
            self._emit(events_file, "run.finished", {"status": str(result.status)})
        if not spec.resumable and self.resumed_from is None:  # a resumed turn never deletes the shared home
            self._release_lock()  # the lock file lives in the home that is about to go
            try:
                robust_rmtree(m.home)
            except OSError as e:
                result.warnings.append(f"could not remove run home {m.home}: {e}")
            result.resumable = False
        return result

    def _finish(self, o: Outcome, t: _Turn) -> RunResult:
        try:
            return self._build_result(o, t)
        except Exception as e:
            result = self._minimal_result(e)
            result.exit_code = t.exit_code
            return result

    async def _turn(self, events_file: TextIO, parser, deadline: float, *, budget_spent: float = 0.0) -> _Turn:
        t = _Turn()
        try:
            async with asyncio.timeout_at(deadline):
                t.budget_hit, t.idle_hit = await self._consume(events_file, parser, budget_spent)
                t.exit_code = await self._proc.wait()
        except TimeoutError:
            t.timed_out = True
            self._proc.kill_tree()
            t.exit_code = await self._proc.wait()
        except Exception as e:  # post-launch problems are data, never raised (CoreDesign §3)
            t.internal = e
            self._proc.kill_tree()
            t.exit_code = await self._proc.wait()
        return t

    async def _consume(self, events_file: TextIO, parser, budget_spent: float) -> tuple[bool, bool]:
        """Pump stdout into events; return (budget_hit, idle_hit)."""
        budget, idle = self._d.spec.budget_usd, self._d.spec.idle_timeout_s
        lines = aiter(self._proc.lines())
        while True:
            try:
                if idle:
                    try:
                        line = await asyncio.wait_for(anext(lines), idle)
                    except TimeoutError:  # wait_for's own timeout: the harness was silent for idle_timeout_s
                        self._proc.kill_tree()
                        return False, True
                else:
                    line = await anext(lines)
            except StopAsyncIteration:
                return False, False
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
            self._note_session(events_file, parser.outcome().session_id)
            cost = parser.running_cost()
            if budget is not None and cost is not None and budget_spent + cost > budget:
                self._proc.kill_tree()
                return True, False

    def _scrub(self, text: str) -> str:
        spec = self._d.spec
        for value in [*spec.secrets.values(), spec.credentials.secret]:
            if value:
                text = text.replace(value, REDACTED)
        return text

    def _build_result(self, o: Outcome, t: _Turn) -> RunResult:
        spec, m = self._d.spec, self.materialized
        if self._cancelled:
            error = RunError("cancelled", "run was cancelled")
        elif t.timed_out:
            error = RunError("timeout", f"no result within timeout_s={spec.timeout_s}")
        elif t.idle_hit:
            error = RunError("idle_timeout", f"no output for idle_timeout_s={spec.idle_timeout_s:g}")
        elif t.budget_hit:
            error = RunError("budget", f"cost {o.cost_usd} exceeded budget_usd={spec.budget_usd}")
        elif self._proc.overflowed:
            error = RunError("stream_overflow", "a stdout line exceeded the stream limit; run was stopped")
        elif t.internal is not None:
            error = RunError("phd_internal", f"{type(t.internal).__name__}: {t.internal}")
        elif o.error is not None:
            error = o.error
        elif t.exit_code != 0:
            error = RunError("harness_crash", f"exit code {t.exit_code}; stderr: {self._scrub(self._proc.stderr_tail())}")
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
            stack_hash=self._stack_hash, status=decide_status(error), exit_code=t.exit_code, error=error,
            result_text=o.result_text, structured_output=structured, cost_usd=o.cost_usd,
            cost_source=o.cost_source, usage=o.usage, num_turns=o.num_turns,
            duration_ms=int((time.monotonic() - self._t0) * 1000), session_id=o.session_id,
            resumable=(spec.resumable or self.resumed_from is not None) and o.session_id is not None,
            resumed_from=self.resumed_from,
            run_dir=str(m.run_dir), home_dir=str(m.home), cwd=str(spec.cwd), project_config=spec.project_config,
            autonomy=spec.autonomy, unsafe=spec.unsafe, credential_kind=spec.credentials.kind,
            started_at=self._started_at.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
            cli_version=o.cli_version, warnings=[*m.warnings, *self._guard_warnings()], raw_final=o.raw_final,
            events_path=str(m.run_dir / "events.jsonl"), stderr_path=str(m.run_dir / "stderr.log"))
