"""Dispatcher: validate → materialize → launch (see RunHandle for collect → ledger)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .._util.fs import is_within
from ..constants import REDACTED
from ..errors import InvalidRunsRoot, NotResumable
from ..harnesses import ModelCatalog, get_adapter
from ..materialize import MaterializedHome, materialize, resume_message
from ..results import LocalLedger, ResultSink, RunResult
from ..spec import Inputs, Spec
from .handle import RunHandle


class Dispatcher:
    """Runs agents described by one `Spec`: dry runs, fresh runs and resumes.

    Errors that can be detected before launch are raised as `PhdError` subclasses. Anything that
    goes wrong after the harness starts is returned as data in `RunResult` (`status`, `error`);
    `spawn`, `resume` and `RunHandle.wait` never raise for a failed run.

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
        again = d.resume(result, "now fix the failing test")
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

    # ---- fresh runs
    def plan(self, inputs: Inputs) -> MaterializedHome:
        """Dry run: allocate a run directory and write the run home, but launch nothing.

        Writes `<run_dir>/home/` and `<run_dir>/run.json` exactly as `start` would. No
        `result.json` is written, so the run never appears in `LocalLedger.list`.

        Args:
            inputs: The task.

        Returns:
            The `MaterializedHome`: `run_id`, `run_dir`, `home`, `argv`, `env` (use
            `redacted_env()` to print it), `stdin` (the first user message) and `warnings`.

        Raises:
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.
        """
        return self._plan(inputs)[0]

    def _plan(self, inputs: Inputs) -> tuple[MaterializedHome, str]:
        stack_hash = self.spec.stack.content_hash()  # pinned at plan time: what actually runs
        run_id, run_dir = self.ledger.allocate()
        m = materialize(self.adapter, self.spec, inputs, run_id=run_id, run_dir=run_dir)
        m.warnings[:0] = self.warnings
        self.ledger.write_record(run_dir, self._record(m, base_argv=m.argv, resumed_from=None))
        return m, stack_hash

    async def start(self, inputs: Inputs) -> RunHandle:
        """Materialize and launch a run; return as soon as the process has started.

        Args:
            inputs: The task.

        Returns:
            A `RunHandle` to stream events from, wait on, or cancel.

        Raises:
            HarnessNotInstalled: The harness CLI is not on PATH (and no `executables` override).
            MissingCredentials: A credential file cannot be read.
            MissingSecret: A placeholder has no value in `Spec.secrets`.

        Example:
            ```python
            handle = await d.start(inputs)
            async for ev in handle.events():
                print(ev.type, ev.data)
            result = await handle.wait()
            ```
        """
        m, stack_hash = self._plan(inputs)
        return await RunHandle.launch(self, m, resumed_from=None, stack_hash=stack_hash)

    def spawn(self, inputs: Inputs) -> RunResult:
        """Blocking wrapper: `start(inputs)` then `wait()`.

        Args:
            inputs: The task.

        Returns:
            The finished `RunResult` (check `status` and `error`).

        Raises:
            RuntimeError: Called while an event loop is running (for example in Jupyter); use
                `await start(...)` there.
            PhdError: Any pre-launch error listed for `start`.
        """
        return _run_sync(self._spawn(inputs))

    async def _spawn(self, inputs: Inputs) -> RunResult:
        return await (await self.start(inputs)).wait()

    # ---- resume
    async def start_resume(self, previous: RunResult | str, msg: str) -> RunHandle:
        """Continue a previous run's session with a new message.

        The resumed run gets a new `run_id` and run directory but reuses the previous run's
        home (and so the harness session), its recorded argv and environment, with fresh
        credentials. Identity and rules come from the session; only `msg` is sent.

        Args:
            previous: A `RunResult` or a run id in this dispatcher's ledger.
            msg: The next user message (must not be blank).

        Returns:
            A `RunHandle` for the resumed run (`resumed_from` is set).

        Raises:
            NotResumable: Unknown run id; different harness; no session id; run home gone
                (`resumable=False` or deleted); missing `run.json`.
            EmptyTask: `msg` is blank.
            HarnessNotInstalled: The harness CLI is not on PATH.
        """
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
        """Blocking wrapper: `start_resume(previous, msg)` then `wait()`.

        Raises:
            RuntimeError: Called while an event loop is running.
            NotResumable: See `start_resume`.
        """
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
