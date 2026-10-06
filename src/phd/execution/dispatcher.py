"""Dispatcher: validate → materialize → launch (see RunHandle for collect → ledger)."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from .._util.fs import is_within, robust_rmtree
from ..errors import InvalidRunsRoot, NotResumable, StackNotFound, UnsupportedFeature
from ..harnesses import ModelCatalog, get_adapter
from ..materialize import MaterializedHome, materialize
from ..results import LocalLedger, ResultSink, RunResult
from ..spec import Inputs, Spec
from .handle import RunHandle
from .session_lock import SessionLock


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
            catalog: Model catalogue to validate against. Defaults to a fresh `ModelCatalog`
                (which runs `codex debug models` / `opencode models` once).
            validate_model: Set `False` to skip model validation entirely.
            executables: Per-harness argv prefix replacing the CLI name, for example
                `{"claude": ["/opt/claude/bin/claude"]}`. Skips the PATH lookup.
            allow_temp_runs_root: Allow a codex `runs_root` under the system temp dir.

        Raises:
            HookUnsupported: A stack hook cannot be expressed for this harness.
            ModelNotSupported: `spec.model` is not in the harness catalogue.
            InvalidRunsRoot: codex with a `runs_root` under the system temp dir.
        """
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
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
            NotResumable: `session_id` cannot be continued (see `start`).
            StackNotFound: The stack's folder no longer exists (removed from the store).
            UnsupportedFeature: `session_id` together with `inputs.rules`.
        """
        return self._plan(inputs, session_id, dry_run=True)[0]

    def _plan(self, inputs: Inputs, session_id: str | None, *,
              dry_run: bool = False) -> tuple[MaterializedHome, str, str | None, SessionLock | None, float]:
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
        return m, stack_hash, resumed_from, lock, _session_cost(prev)

    def _lock_session(self, home: Path, run_id: str, run_dir: Path, session_id: str) -> SessionLock:
        """Lock the session's shared home; when another live run holds it, remove the still-empty run
        directory and raise `NotResumable` before anything in the home is touched."""
        try:
            return SessionLock(home, run_id).acquire(f"session {session_id!r}")
        except NotResumable:
            robust_rmtree(run_dir)
            raise

    def _find_session(self, inputs: Inputs, session_id: str) -> RunResult:
        """The latest finished run of `session_id`, checked to be continuable by this spec; writes nothing."""
        if inputs.rules is not None:
            raise UnsupportedFeature("rules cannot be passed when resuming a session; "
                                     "they are part of the session's system prompt")
        prev = self.ledger.latest_for_session(session_id)
        if prev is None:
            raise NotResumable(f"no finished run with session {session_id!r} in {self.ledger.root}")
        if prev.harness != self.spec.harness:
            raise NotResumable(f"session {session_id!r} belongs to {prev.harness!r} (run {prev.run_id}); "
                               f"this dispatcher runs {self.spec.harness!r}")
        home = Path(prev.home_dir)
        self._check_home_contained(session_id, home)
        if not home.is_dir():
            raise NotResumable(f"session {session_id!r}: run home {home} no longer exists "
                               "(resumable=False or cleaned up)")
        self._check_no_links(session_id, home)
        if Path(prev.cwd) != self.spec.cwd:
            raise NotResumable(f"session {session_id!r} ran in cwd {prev.cwd}; this spec's cwd is {self.spec.cwd} "
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
        dispatcher's ledger and re-materializes its home from **this** spec: skills, identity,
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
            A `RunHandle` to stream events from, wait on, or cancel.

        Raises:
            HarnessNotInstalled: The harness CLI is not on PATH (and no `executables` override).
            StackNotFound: The stack's folder no longer exists (removed from the store).
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
            NotResumable: No finished run has `session_id`; it belongs to another harness; its
                home no longer exists; it ran in a different `cwd`; or another live run is using
                the session right now (`<home>/.phd-session.lock`; a lock left by a dead process is
                taken over automatically).
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
        m, stack_hash, resumed_from, lock, cost_before = self._plan(inputs, session_id)
        try:
            return await RunHandle.launch(self, m, resumed_from=resumed_from, stack_hash=stack_hash, lock=lock,
                                          session_id=session_id, session_cost_before=cost_before)
        except BaseException:  # the harness never started: free the home again
            if lock is not None:
                lock.release()
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
            The finished `RunResult` (check `status` and `error`).

        Raises:
            RuntimeError: Called while an event loop is running (for example in Jupyter); use
                `await start(...)` there.
            PhdError: Any pre-launch error listed for `start`.
        """
        return _run_sync(self._spawn(inputs, session_id))

    async def _spawn(self, inputs: Inputs, session_id: str | None) -> RunResult:
        return await (await self.start(inputs, session_id=session_id)).wait()

    def _record(self, m: MaterializedHome, *, session_id: str | None, resumed_from: str | None) -> dict:
        return {"run_id": m.run_id, "harness": self.spec.harness, "model": self.spec.model,
                "cwd": str(self.spec.cwd), "argv": m.argv, "env": m.redacted_env(), "stdin": m.stdin,
                "warnings": m.warnings, "session_id": session_id, "resumed_from": resumed_from, "home": str(m.home),
                "rules_text": m.rules_text}


def _session_cost(prev: RunResult | None) -> float:
    """What the session had cost before this turn, for harnesses that report cumulative cost: the previous
    run's `session_cost_usd`, else (a record written before that field existed) its `cost_usd`."""
    if prev is None:
        return 0.0
    if prev.session_cost_usd is not None:
        return prev.session_cost_usd
    return prev.cost_usd or 0.0


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("spawn() cannot run inside an event loop; use `await dispatcher.start(...)` instead")
