"""RunHandle: one live run — pump the harness stream into events, build the RunResult, feed the sinks."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TextIO

from .._util.fs import robust_rmtree
from .._util.shell import resolve_argv
from ..constants import REDACTED
from ..harnesses import Outcome
from ..materialize import MaterializedHome
from ..results import Event, RunError, RunResult, Usage
from ..results.model import decide_status
from ..spec.output_schema import extract_json, validate_answer
from .process import HarnessProcess, child_env

REPAIR_PROMPT = "Your final answer did not match the required JSON Schema: {error}. Reply with only the corrected JSON."
REPAIR_MIN_TIME_S = 1.0  # a repair turn needs at least this much of timeout_s left

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
                 lock: SessionLock | None = None, session_id: str | None = None, session_cost_before: float = 0.0):
        self._d = dispatcher
        # Adapters whose harness reports the session's cumulative cost (claude on --resume): PHD subtracts
        # what the session had cost before each invocation to get the run's own cost.
        self._cumulative = bool(getattr(dispatcher.adapter, "cumulative_cost", False))
        self._cost_before = session_cost_before
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
        self._session_warnings: list[str] = []  # problems recording the session; added to the result
        self._started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()

    @classmethod
    async def launch(cls, d: Dispatcher, m: MaterializedHome, *, resumed_from: str | None,
                     stack_hash: str, lock: SessionLock | None = None,
                     session_id: str | None = None, session_cost_before: float = 0.0) -> RunHandle:
        handle = cls(d, m, resumed_from, stack_hash, lock, session_id, session_cost_before)
        handle._proc = handle._new_process(m.argv, m.stdin)
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
            self._announce_session(sink, session_id)

    def _announce_session(self, sink: TextIO, session_id: str) -> None:
        """Record the session in the run directory, then emit `session.started`. The record is what lets a run that
        dies before `result.json` be resumed; failing to write it never breaks the run."""
        spec, m = self._d.spec, self.materialized
        try:
            self._d.ledger.write_session(m.run_dir, {
                "session_id": session_id, "harness": spec.harness, "cwd": str(spec.cwd), "home": str(m.home),
                "stack": spec.stack.ref, "resumable": spec.resumable, "started_at": self._started_at.isoformat()})
        except OSError as e:
            self._session_warnings.append(f"could not write the session record: {e}")
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
            duration_ms=int((time.monotonic() - self._t0) * 1000), session_id=self.session_id,
            resumable=(spec.resumable or self.resumed_from is not None) and self.session_id is not None,
            session_cost_usd=(self._cost_before or None) if self._cumulative else None,
            resumed_from=self.resumed_from, run_dir=str(m.run_dir), home_dir=str(m.home), cwd=str(spec.cwd),
            project_config=spec.project_config, autonomy=spec.autonomy, unsafe=spec.unsafe,
            credential_kind=spec.credentials.kind, started_at=self._started_at.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(), warnings=[*m.warnings, *self._guard_warnings(), *self._session_warnings],
            events_path=str(m.run_dir / "events.jsonl"), stderr_path=str(m.run_dir / "stderr.log"))

    async def _run_to_result(self) -> RunResult:
        spec, m = self._d.spec, self.materialized
        deadline = asyncio.get_running_loop().time() + spec.timeout_s
        with open(m.run_dir / "events.jsonl", "a", encoding="utf-8") as events_file:
            self._emit(events_file, "run.started", {"argv": m.argv, "resumed_from": self.resumed_from})
            if self._resume_session:
                self._announce_session(events_file, self._resume_session)
            parser = self._d.adapter.parser()
            turn = await self._turn(events_file, parser, deadline, cost_before=self._cost_before)
            reported = parser.outcome()
            own = self._own_cost(reported, self._cost_before)
            result = self._finish(own, turn, session_cost=self._session_cost(reported, self._cost_before))
            if (spec.schema_repair and result.error is not None and result.error.kind == "schema"
                    and result.session_id is not None and not self._cancelled):
                result = await self._repair(events_file, result, own, reported.cost_usd, deadline)
            result.warnings += self._session_warnings
            self._emit(events_file, "run.finished", {"status": str(result.status)})
        if not spec.resumable and self.resumed_from is None:  # a resumed turn never deletes the shared home
            self._release_lock()  # the lock file lives in the home that is about to go
            try:
                robust_rmtree(m.home)
            except OSError as e:
                result.warnings.append(f"could not remove run home {m.home}: {e}")
            result.resumable = False
        return result

    def _check_answer(self, o: Outcome) -> tuple[Any, RunError | None]:
        """(parsed answer, schema error or None) for a run with `output_schema`."""
        value, present = o.structured_output, o.structured_output is not None
        if not present and o.result_text:
            try:
                value, present = extract_json(o.result_text), True
            except ValueError:
                pass
        if not present:
            return None, RunError("schema", "final answer is not JSON")
        problem = validate_answer(self._d.spec.output_schema, value, scrub=self._scrub)
        return value, (None if problem is None else RunError("schema", problem))

    def _new_process(self, argv: list[str], stdin_text: str) -> HarnessProcess:
        m = self.materialized
        return HarnessProcess(self._resolve(argv), env=child_env(m.env), cwd=self._d.spec.cwd,
                              stdin_text=stdin_text, stderr_path=m.run_dir / "stderr.log")

    def _own_cost(self, o: Outcome, before: float) -> Outcome:
        """`o` with the cost of this invocation alone (the reported figure minus `before` for a cumulative adapter)."""
        if not self._cumulative or o.cost_usd is None:
            return o
        return dataclasses.replace(o, cost_usd=_own_cost(o.cost_usd, before))

    def _session_cost(self, reported: Outcome, before: float) -> float | None:
        """The session's cumulative cost after this invocation: what it reported, else the baseline carried forward
        (a turn killed before its result line reports nothing; the next resume must not be charged the session)."""
        if not self._cumulative:
            return None
        return reported.cost_usd if reported.cost_usd is not None else (before or None)

    async def _repair(self, events_file: TextIO, first: RunResult, first_outcome: Outcome,
                      first_reported: float | None, deadline: float) -> RunResult:
        spec = self._d.spec
        if self._cancelled:
            return first
        if deadline - asyncio.get_running_loop().time() < REPAIR_MIN_TIME_S:
            first.warnings.append("schema repair skipped: timeout_s nearly spent")
            return first
        spent = first.cost_usd or 0.0
        remaining = None if spec.budget_usd is None else round(spec.budget_usd - spent, 6)
        if remaining is not None and remaining <= 0:
            first.warnings.append("schema repair skipped: budget_usd spent")
            return first
        try:
            return await self._repair_turn(events_file, first, first_outcome, first_reported, deadline,
                                           spent, remaining)
        except Exception as e:  # keep the first turn's result: its answer was already wrong, not lost
            self._kill_if_running()
            first.warnings.append(f"schema repair turn failed: {type(e).__name__}: {e}")
            return first

    async def _repair_turn(self, events_file: TextIO, first: RunResult, first_outcome: Outcome,
                           first_reported: float | None, deadline: float, spent: float,
                           remaining: float | None) -> RunResult:
        spec, m = self._d.spec, self.materialized
        argv = m.argv if self._resume_session is not None else self._d.adapter.resume_argv(m.argv, first.session_id, spec)
        if remaining is not None:
            argv = _with_budget(argv, remaining)
        warnings = self._guard_warnings()
        self._proc.close()
        try:
            self._proc = self._new_process(argv, REPAIR_PROMPT.format(error=first.error.message))
            await self._proc.start()
        except Exception as e:
            first.warnings.append(f"schema repair turn could not start: {type(e).__name__}: {e}")
            return first
        self._emit(events_file, "schema.repair", {"error": first.error.message})
        if self._cancelled:  # a cancel during start() found no process to kill
            self._proc.kill_tree()
        # The session's cumulative cost before the repair turn is what the first turn reported.
        before = first_reported if first_reported is not None else self._cost_before
        parser = self._d.adapter.parser()
        turn = await self._turn(events_file, parser, deadline, budget_spent=spent, cost_before=before)
        reported = parser.outcome()
        session_cost = self._session_cost(reported, before) if reported.cost_usd is not None else first.session_cost_usd
        result = self._finish(_merge(first_outcome, self._own_cost(reported, before)), turn, session_cost=session_cost)
        result.warnings += [w for w in warnings if w not in result.warnings]  # the first turn's guard warning
        result.warnings.append("schema repair turn used")
        if result.error is not None and result.error.kind == "schema":
            result.error = RunError("schema", "after one repair turn: " + result.error.message)
        return result

    def _finish(self, o: Outcome, t: _Turn, *, session_cost: float | None = None) -> RunResult:
        try:
            result = self._build_result(o, t)
            result.session_cost_usd = session_cost
            return result
        except Exception as e:
            result = self._minimal_result(e)
            result.exit_code = t.exit_code
            return result

    async def _turn(self, events_file: TextIO, parser, deadline: float, *, budget_spent: float = 0.0,
                    cost_before: float = 0.0) -> _Turn:
        t = _Turn()
        try:
            async with asyncio.timeout_at(deadline):
                t.budget_hit, t.idle_hit = await self._consume(events_file, parser, budget_spent, cost_before)
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

    async def _consume(self, events_file: TextIO, parser, budget_spent: float,
                       cost_before: float = 0.0) -> tuple[bool, bool]:
        """Pump stdout into events; return (budget_hit, idle_hit).

        The budget check counts `budget_spent` (earlier turns of this run) plus this invocation's own cost."""
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
            if cost is not None and self._cumulative:
                cost = _own_cost(cost, cost_before)
            if budget is not None and cost is not None and budget_spent + cost > budget:
                self._proc.kill_tree()
                return True, False

    def _scrub(self, text: str) -> str:
        spec = self._d.spec
        for value in [*spec.secrets.values(), spec.credentials.secret]:
            if value:
                # also the escaped renderings a JSON or repr() message would show (longest first)
                for form in sorted({value, json.dumps(value)[1:-1], repr(value)[1:-1]}, key=len, reverse=True):
                    text = text.replace(form, REDACTED)
        return text

    def _build_result(self, o: Outcome, t: _Turn) -> RunResult:
        spec, m = self._d.spec, self.materialized
        if self._cancelled:
            error = RunError("cancelled", "run was cancelled")
        elif t.timed_out:
            error = RunError("timeout", f"no result within timeout_s={spec.timeout_s}")
        elif t.idle_hit:
            error = RunError("idle", f"no output for idle_timeout_s={spec.idle_timeout_s:g}")
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
        if spec.output_schema is not None:
            checked, schema_err = self._check_answer(o)
            if checked is not None:
                structured = checked
            if error is None:
                error = schema_err
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


def _own_cost(reported: float, before: float) -> float:
    """One invocation's own cost from a cumulative figure. A figure below the baseline (stale baseline, or a harness
    reporting per invocation) is itself the turn's cost."""
    return reported if reported < before else round(reported - before, 10)


def _merge(first: Outcome, second: Outcome) -> Outcome:
    """The repair turn's outcome with usage, cost and turns summed over both turns."""
    usage = Usage()
    usage.add(first.usage)
    usage.add(second.usage)
    costs = [c for c in (first.cost_usd, second.cost_usd) if c is not None]
    turns = [n for n in (first.num_turns, second.num_turns) if n is not None]
    return dataclasses.replace(
        second, usage=usage, cost_usd=sum(costs) if costs else None,
        cost_source=second.cost_source if len(costs) == 2 else "unknown",  # one turn unpriced: a sum undercounts
        num_turns=sum(turns) if turns else None, session_id=second.session_id or first.session_id,
        cli_version=second.cli_version or first.cli_version)


def _with_budget(argv: list[str], remaining: float) -> list[str]:
    """`argv` with the value after `--max-budget-usd` (if present) replaced by `remaining`."""
    out = list(argv)
    if "--max-budget-usd" in out:
        out[out.index("--max-budget-usd") + 1] = str(round(remaining, 6))
    return out
