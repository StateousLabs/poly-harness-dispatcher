"""Dispatcher: validate → materialize → launch (see RunHandle for collect → ledger)."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from .._util.fs import is_within, robust_rmtree
from ..errors import InvalidRunsRoot, NotResumable, StackNotFound, UnsupportedFeature
from ..harnesses import ModelCatalog, get_adapter
from ..materialize import MaterializedHome, materialize
from ..results import LocalLedger, ResultSink, RunResult, SessionRef
from ..spec import Inputs, Spec
from .handle import RunHandle
from .session_lock import SessionLock


class _SessionUnavailable(NotResumable):
    """The requested session is gone (unknown, other harness or cwd, home removed, not resumable). `start` turns it
    into a failed run with `kind="session"`; `plan` lets it propagate. Busy sessions and integrity problems stay
    plain `NotResumable`."""


class Dispatcher:
    """Runs agents described by one `Spec`: dry runs, fresh runs and resumed sessions.

    Errors that can be detected before launch are raised as `PhdError` subclasses. Anything that
    goes wrong after the harness starts is returned as data in `RunResult` (`status`, `error`);
    `spawn` and `RunHandle.wait` never raise for a failed run.

    Every run allocates `<runs_root>/<run_id>/` and writes `run.json` there before launch; the
    result is written to `result.json` and then handed to each extra sink.

    Attributes:
        spec: The spec this dispatcher runs.
        ledger: The `LocalLedger` at `runs_root`; always the first sink.
        sinks: `[ledger, *sinks]`.
        warnings: Pre-launch warnings (adapter checks and model catalogue); copied into every
            run's `warnings`.

    Example:
        ```python
        d = Dispatcher(spec)
        result = d.spawn(Inputs(brief="task.md"))          # blocking
        again = d.spawn(Inputs(msg="now fix the failing test"), session_id=result.session_id)
        ```
    """

    def __init__(self, spec: Spec, *, runs_root: str | Path | None = None, sinks: Sequence[ResultSink] = (),
                 catalog: ModelCatalog | None = None, validate_model: bool = True,
                 executables: dict[str, list[str]] | None = None, allow_temp_runs_root: bool = False):
        """Validate the spec against its harness and prepare the ledger.

        Args:
            spec: The run description.
            runs_root: Ledger directory. Defaults to `~/.phd/runs`.
            sinks: Extra `ResultSink`s that receive every `RunResult` after the local ledger.
            catalog: Model catalogue to validate against. Defaults to a `ModelCatalog` that uses
                `executables` and shares its codex / opencode lookups (`codex debug models` /
                `opencode models`) with every other dispatcher in the process. Those lookups are
                slow, so they do not run here: they run in a worker thread on the first `start()`
                (or synchronously in `plan()`).
            validate_model: Set `False` to skip model validation entirely.
            executables: Per-harness argv prefix replacing the CLI name (also for the codex / opencode model
                catalogue command: a wrapper must support `debug models` / `models`, or use `validate_model=False`), for example
                `{"claude": ["/opt/claude/bin/claude"]}`. Skips the PATH lookup.
            allow_temp_runs_root: Allow a codex `runs_root` under the system temp dir.

        Raises:
            HookUnsupported: A stack hook cannot be expressed for this harness.
            ModelNotSupported: Claude only: `spec.model` is not a known claude model. For codex and opencode
                the check is lazy and `start()` / `run()` / `plan()` raise it.
            InvalidRunsRoot: codex with a `runs_root` under the system temp dir.
        """
        self.spec = spec
        self.adapter = get_adapter(spec.harness)
        self.ledger = LocalLedger(runs_root)
        self.sinks: list[ResultSink] = [self.ledger, *sinks]
        self.executables = dict(executables or {})
        self.warnings: list[str] = list(self.adapter.validate(spec))
        self._catalog = (catalog or ModelCatalog(executables=self.executables)) if validate_model else None
        self._model_checked = self._catalog is None
        self._model_lock = threading.Lock()
        if self._catalog is not None and self._catalog.is_static(spec.harness):  # no subprocess: check now
            self._check_model()
        temp = Path(tempfile.gettempdir())
        if spec.harness == "codex" and not allow_temp_runs_root and is_within(self.ledger.root, temp):
            raise InvalidRunsRoot(f"codex refuses a CODEX_HOME under the system temp dir; choose a runs_root outside {temp}")

    def _check_model(self) -> None:
        """Validate `spec.model` against the catalogue once; its warnings join `self.warnings`. Blocks while
        a codex / opencode catalogue loads, so async code reaches it from a worker thread (via `_plan`)."""
        with self._model_lock:
            if self._model_checked:
                return
            self.warnings += self._catalog.validate(self.spec.harness, self.spec.model)
            self._model_checked = True

    # ---- runs (fresh or resumed)
    def plan(self, inputs: Inputs, *, session_id: str | None = None) -> MaterializedHome:
        """Dry run: allocate a run directory and write the run home, but launch nothing.

        Writes the run home and `<run_dir>/run.json` exactly as `start` would. No `result.json` is
        written, so the run never appears in `LocalLedger.list`. With `session_id` this previews a
        resumed turn without modifying the session: the session checks run, the session's rules are
        applied and `argv` carries the harness's resume flag for that session, but the files are
        written into this plan's own `<run_dir>/home`, never into the session's shared home (so `env`
        points at the preview home, not the session's).

        Args:
            inputs: The task.
            session_id: Plan a turn that continues this harness session (see `start`).

        Returns:
            The `MaterializedHome`: `run_id`, `run_dir`, `home`, `argv`, `env` (use
            `redacted_env()` to print it), `stdin` (the first user message) and `warnings`.

        Raises:
            ModelNotSupported: `spec.model` is not in the harness catalogue (loaded synchronously here).
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
            NotResumable: `session_id` cannot be continued: the session is busy or its home is tampered with (see
                `start`), and, unlike `start`, also when no run has the session, it belongs to another harness, it
                ran in another `cwd`, its home is gone, or its run crashed with `resumable=False` (there is no
                run to return from a dry run).
            StackNotFound: The stack's folder no longer exists (removed from the store).
            UnsupportedFeature: `session_id` together with `inputs.rules`.
        """
        return self._plan(inputs, session_id, dry_run=True)[0]

    def _plan(self, inputs: Inputs, session_id: str | None, *,
              dry_run: bool = False) -> tuple[MaterializedHome, str, str | None, SessionLock | None, float]:
        """Blocking (catalogue lookup, stack copy, ledger writes): `start` runs it in a worker thread, so it must
        not touch the event loop or any asyncio object."""
        self._check_model()
        if not self.spec.stack.root.is_dir():
            raise StackNotFound(f"stack {self.spec.stack.ref}: {self.spec.stack.root} no longer exists "
                                "(removed from the store?)")
        prev = self._find_session(inputs, session_id) if session_id is not None else None
        stack_hash = self.spec.stack.content_hash()  # pinned at plan time: what actually runs
        run_id, run_dir = self.ledger.allocate()
        home = Path(prev.home_dir) if prev and not dry_run else None  # a dry run never touches the session
        # A resumed turn locks the shared home before rewriting it; a dry run never locks.
        lock = self._lock_session(home, run_id, run_dir, session_id) if home is not None else None
        try:
            rules_text = self.ledger.read_record(prev.run_id).get("rules_text") if prev else None  # session's rules
            m = materialize(self.adapter, self.spec, inputs, run_id=run_id, run_dir=run_dir, home=home,
                            rules_text=rules_text)
            if lock is None and not dry_run:  # a fresh run locks its new home for as long as it runs
                lock = SessionLock(m.home, run_id).acquire(f"run home {m.home}")
            warnings = list(self.warnings)
            if prev:
                m.argv = self.adapter.resume_argv(m.argv, session_id, self.spec)
                if prev.stack != self.spec.stack.ref:
                    warnings.append(f"session {session_id} ran stack {prev.stack}; this turn runs {self.spec.stack.ref}")
            m.warnings[:0] = warnings
            resumed_from = prev.run_id if prev else None
            self.ledger.write_record(run_dir, self._record(m, session_id=session_id, resumed_from=resumed_from))
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        return m, stack_hash, resumed_from, lock, prev.cost_before if prev else 0.0

    def _lock_session(self, home: Path, run_id: str, run_dir: Path, session_id: str) -> SessionLock:
        """Lock the session's shared home; when another live run holds it, remove the still-empty run
        directory and raise `NotResumable` before anything in the home is touched."""
        try:
            return SessionLock(home, run_id).acquire(f"session {session_id!r}")
        except NotResumable:
            robust_rmtree(run_dir)
            raise

    def _find_session(self, inputs: Inputs, session_id: str) -> SessionRef:
        """The run to resume `session_id` from (the latest finished one, else the newest crashed one), checked to
        be continuable by this spec; writes nothing."""
        if inputs.rules is not None:
            raise UnsupportedFeature("rules cannot be passed when resuming a session; "
                                     "they are part of the session's system prompt")
        prev = self.ledger.find_session(session_id)
        if prev is None:
            raise _SessionUnavailable(f"no run with session {session_id!r} in {self.ledger.root}")
        if prev.harness != self.spec.harness:
            raise _SessionUnavailable(f"session {session_id!r} belongs to {prev.harness!r} (run {prev.run_id}); "
                               f"this dispatcher runs {self.spec.harness!r}")
        if not prev.finished and not prev.resumable:
            raise _SessionUnavailable(f"session {session_id!r}: its run {prev.run_id} was not resumable (resumable=False) "
                               "and did not finish")
        home = Path(prev.home_dir)
        self._check_home_contained(session_id, home)
        if not home.is_dir():
            raise _SessionUnavailable(f"session {session_id!r}: run home {home} no longer exists "
                               "(resumable=False or cleaned up)")
        self._check_no_links(session_id, home)
        if Path(prev.cwd) != self.spec.cwd:
            raise _SessionUnavailable(f"session {session_id!r} ran in cwd {prev.cwd}; this spec's cwd is {self.spec.cwd} "
                               "(harnesses key sessions by directory)")
        if not (self.ledger.root / prev.run_id / "run.json").is_file():
            raise NotResumable(f"session {session_id!r}: run {prev.run_id} has no run.json (its rules are unknown)")
        return prev

    def _check_home_contained(self, session_id: str, home: Path) -> None:
        """`home_dir` comes from a result.json; only accept `<ledger root>/<run_id>/home` of a run in this ledger,
        since resuming deletes PHD-owned paths under it."""
        if _is_link(home):
            raise NotResumable(f"session {session_id!r}: run home {home} is a symlink or junction")
        resolved = home.resolve()
        if (resolved.name != "home" or resolved.parent.parent != self.ledger.root
                or not (resolved.parent / "run.json").is_file()):
            raise NotResumable(f"session {session_id!r}: run home {home} is not a run home in {self.ledger.root}")

    def _check_no_links(self, session_id: str, home: Path) -> None:
        """Refuse owned paths (or their parents inside the home) that are links, so no deletion leaves the home."""
        for owned in [home / "stack", home / "phd", *self.adapter.owned_paths(home)]:
            p = owned
            while p != home:
                if _is_link(p):
                    raise NotResumable(f"session {session_id!r}: {p} is a symlink or junction")
                p = p.parent

    async def start(self, inputs: Inputs, *, session_id: str | None = None) -> RunHandle:
        """Materialize and launch a run; return as soon as the process has started.

        With `session_id`, the run continues that harness session (a value from a previous
        `RunResult.session_id`). PHD finds the latest finished run of the session in this
        dispatcher's ledger (or, if none finished, the newest run that crashed after reporting it) and re-materializes its home from **this** spec: skills, identity,
        MCP servers, hooks, settings and credentials all come from the new spec, while the
        harness's session data in the home is kept. The run gets its own `run_id` and run
        directory; `RunResult.resumed_from` names the run the session was found in. The first
        message is built from `inputs` as for a fresh run. The rules the session started with
        (recorded as `rules_text` in each run's `run.json`) are re-applied to the system prompt on
        every resumed turn; passing new `inputs.rules` is not allowed.

        Args:
            inputs: The task.
            session_id: Continue this harness session instead of starting a new one.

        Returns:
            A `RunHandle` to stream events from, wait on, or cancel. When `session_id` cannot be resumed
            (unknown to the ledger, another harness, another `cwd`, its home gone, or a crashed run with
            `resumable=False`) nothing is launched: the handle is already done and its result is a failed run
            with `error.kind == "session"` (the reason is in `error.message`). Such a run still gets a run id,
            a run directory, `result.json` and `events.jsonl` (`run.started`, `run.finished`), and reaches the sinks.

        A caller cancelled while `start()` is still planning leaves nothing behind: once the worker thread finishes, the
        session lock is released and a fresh run's directory is removed (a resumed turn's shared home is kept).

        Nothing here blocks the event loop: the model catalogue lookup (first call only), the home
        materialization and the ledger writes run in worker threads.

        Raises:
            ModelNotSupported: `spec.model` is not in the harness catalogue (codex / opencode; raised before
                anything is launched or written).
            HarnessNotInstalled: The harness CLI is not on PATH (and no `executables` override).
            StackNotFound: The stack's folder no longer exists (removed from the store).
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
            NotResumable: Another live run is using the session right now (`<home>/.phd-session.lock`; a lock
                left by a dead process is taken over automatically), or the session's recorded home is not a
                run home of this ledger or contains a link (tampering), or its `run.json` is gone.
            UnsupportedFeature: `session_id` together with `inputs.rules` (rules are part of the
                session's system prompt).

        Example:
            ```python
            handle = await d.start(inputs)
            async for ev in handle.events():
                print(ev.type, ev.data)
            result = await handle.wait()
            nxt = await (await d.start(Inputs(msg="now fix it"), session_id=result.session_id)).wait()
            ```
        """
        try:
            m, stack_hash, resumed_from, lock, cost_before = await self._plan_off_loop(inputs, session_id)
        except _SessionUnavailable as e:
            return await self._unavailable_session(session_id, str(e))
        try:
            return await RunHandle.launch(self, m, resumed_from=resumed_from, stack_hash=stack_hash, lock=lock,
                                          session_id=session_id, session_cost_before=cost_before)
        except BaseException:  # the harness never started: free the home again
            if lock is not None:
                lock.release()
            raise

    async def _plan_off_loop(self, inputs: Inputs, session_id: str | None):
        """`_plan` in a worker thread. If this task is cancelled meanwhile, the thread still finishes; the session
        lock it took is released as soon as it does (nothing else owns it), and a fresh run's directory (with its
        home) is removed. A resumed turn's shared home is never touched."""
        fut = asyncio.ensure_future(asyncio.to_thread(self._plan, inputs, session_id))

        def cleanup(m: MaterializedHome, lock: SessionLock | None) -> None:
            if lock is not None:
                lock.release()
            if session_id is None:
                robust_rmtree(m.run_dir)

        def discard(f: asyncio.Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            m, _, _, lock, _ = f.result()
            asyncio.get_running_loop().run_in_executor(None, cleanup, m, lock)  # off the loop: it deletes a tree

        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            fut.add_done_callback(discard)
            raise

    @contextlib.asynccontextmanager
    async def run(self, inputs: Inputs, *, session_id: str | None = None) -> AsyncIterator[RunHandle]:
        """`start()` as an async context manager: leaving the block ends the run.

        On exit for any reason (the block ended, an exception, task cancellation), a run that has
        not finished is cancelled and PHD waits for the harness to die before the exception goes
        on. The result is recorded in the ledger as usual (status `cancelled`).

        Args:
            inputs: The task.
            session_id: Continue this harness session (see `start`).

        Yields:
            The `RunHandle`. A session that cannot be resumed does not raise: the handle is already done and its
            result is a failed run with `error.kind == "session"` (see `start`).

        Raises:
            PhdError: Any pre-launch error listed for `start`, raised on entering the block.

        Example:
            ```python
            async with d.run(inputs) as handle:
                async for ev in handle.events():
                    print(ev.type)
                result = await handle.wait()
            ```
        """
        handle = await self.start(inputs, session_id=session_id)
        try:
            yield handle
        finally:
            if not handle.done:
                handle._request_cancel()  # synchronous: the kill is issued before anything can interrupt us
                cancelled_again = False
                while not handle.done:
                    try:
                        await handle.wait()
                    except asyncio.CancelledError:
                        cancelled_again = True  # keep waiting: the kill is already issued, so this is bounded
                if cancelled_again:
                    raise asyncio.CancelledError

    def spawn(self, inputs: Inputs, *, session_id: str | None = None) -> RunResult:
        """Blocking wrapper: `start(inputs, session_id=...)` then `wait()`.

        Args:
            inputs: The task.
            session_id: Continue this harness session (see `start`).

        Returns:
            The finished `RunResult` (check `status` and `error`). A session that cannot be resumed does not
            raise: the result is a failed run with `error.kind == "session"` (see `start`).

        Raises:
            RuntimeError: Called while an event loop is running (for example in Jupyter); use
                `await start(...)` there.
            PhdError: Any pre-launch error listed for `start`.
        """
        return _run_sync(self._spawn(inputs, session_id))

    async def _spawn(self, inputs: Inputs, session_id: str | None) -> RunResult:
        return await (await self.start(inputs, session_id=session_id)).wait()

    async def _unavailable_session(self, session_id: str, reason: str) -> RunHandle:
        """A finished, failed run (`error.kind == "session"`) for a session that cannot be resumed; nothing is
        launched, no home is created. It has a run directory with `run.json`, `events.jsonl` and `result.json`."""
        def write() -> tuple[str, str, Path]:
            stack_hash = self.spec.stack.content_hash()
            run_id, run_dir = self.ledger.allocate()
            self.ledger.write_record(run_dir, {
                "run_id": run_id, "harness": self.spec.harness, "model": self.spec.model, "cwd": str(self.spec.cwd),
                "argv": [], "env": {}, "stdin": "", "warnings": list(self.warnings), "session_id": session_id,
                "resumed_from": None, "home": "", "rules_text": None})
            return stack_hash, run_id, run_dir

        stack_hash, run_id, run_dir = await asyncio.to_thread(write)
        return await RunHandle.unlaunched(self, run_id, run_dir, stack_hash, reason)

    def _record(self, m: MaterializedHome, *, session_id: str | None, resumed_from: str | None) -> dict:
        return {"run_id": m.run_id, "harness": self.spec.harness, "model": self.spec.model,
                "cwd": str(self.spec.cwd), "argv": m.argv, "env": m.redacted_env(), "stdin": m.stdin,
                "warnings": m.warnings, "session_id": session_id, "resumed_from": resumed_from, "home": str(m.home),
                "rules_text": m.rules_text}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("spawn() cannot run inside an event loop; use `await dispatcher.start(...)` instead")
