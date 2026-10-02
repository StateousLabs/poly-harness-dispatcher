"""Dispatcher: validate → materialize → launch (see RunHandle for collect → ledger)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .._util.fs import is_within
from ..errors import InvalidRunsRoot, NotResumable, UnsupportedFeature
from ..harnesses import ModelCatalog, get_adapter
from ..materialize import MaterializedHome, materialize
from ..results import LocalLedger, ResultSink, RunResult
from ..spec import Inputs, Spec
from .handle import RunHandle


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
            UnsupportedFeature: The spec uses a feature this harness lacks (opencode
                `output_schema`).
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
        written, so the run never appears in `LocalLedger.list`. With `session_id` this is a dry run
        of a resumed turn: the session's home is re-materialized from this spec (as `start` would do)
        and `argv` carries the harness's resume flag.

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
            UnsupportedFeature: `session_id` together with `inputs.rules`.
        """
        return self._plan(inputs, session_id)[0]

    def _plan(self, inputs: Inputs, session_id: str | None) -> tuple[MaterializedHome, str, str | None]:
        prev = self._find_session(inputs, session_id) if session_id is not None else None
        stack_hash = self.spec.stack.content_hash()  # pinned at plan time: what actually runs
        run_id, run_dir = self.ledger.allocate()
        home = Path(prev.home_dir) if prev else None
        m = materialize(self.adapter, self.spec, inputs, run_id=run_id, run_dir=run_dir, home=home)
        warnings = list(self.warnings)
        if prev:
            m.argv = self.adapter.resume_argv(m.argv, session_id, self.spec)
            if prev.stack != self.spec.stack.ref:
                warnings.append(f"session {session_id} ran stack {prev.stack}; this turn runs {self.spec.stack.ref}")
        m.warnings[:0] = warnings
        resumed_from = prev.run_id if prev else None
        self.ledger.write_record(run_dir, self._record(m, session_id=session_id, resumed_from=resumed_from))
        return m, stack_hash, resumed_from

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
        if not Path(prev.home_dir).is_dir():
            raise NotResumable(f"session {session_id!r}: run home {prev.home_dir} no longer exists "
                               "(resumable=False or cleaned up)")
        if Path(prev.cwd) != self.spec.cwd:
            raise NotResumable(f"session {session_id!r} ran in cwd {prev.cwd}; this spec's cwd is {self.spec.cwd} "
                               "(harnesses key sessions by directory)")
        return prev

    async def start(self, inputs: Inputs, *, session_id: str | None = None) -> RunHandle:
        """Materialize and launch a run; return as soon as the process has started.

        With `session_id`, the run continues that harness session (a value from a previous
        `RunResult.session_id`). PHD finds the latest finished run of the session in this
        dispatcher's ledger and re-materializes its home from **this** spec: skills, identity,
        MCP servers, hooks, settings and credentials all come from the new spec, while the
        harness's session data in the home is kept. The run gets its own `run_id` and run
        directory; `RunResult.resumed_from` names the run the session was found in. The first
        message is built from `inputs` as for a fresh run; `inputs.rules` is not allowed.

        Args:
            inputs: The task.
            session_id: Continue this harness session instead of starting a new one.

        Returns:
            A `RunHandle` to stream events from, wait on, or cancel.

        Raises:
            HarnessNotInstalled: The harness CLI is not on PATH (and no `executables` override).
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
            NotResumable: No finished run has `session_id`; it belongs to another harness; its
                home no longer exists; or it ran in a different `cwd`.
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
        m, stack_hash, resumed_from = self._plan(inputs, session_id)
        return await RunHandle.launch(self, m, resumed_from=resumed_from, stack_hash=stack_hash)

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
                "warnings": m.warnings, "session_id": session_id, "resumed_from": resumed_from, "home": str(m.home)}


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("spawn() cannot run inside an event loop; use `await dispatcher.start(...)` instead")
