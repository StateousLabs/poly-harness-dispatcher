"""SessionRef: where a resumable harness session lives, found through the ledger."""

from dataclasses import dataclass

from .model import RunResult


@dataclass(frozen=True)
class SessionRef:
    """The run a session is resumed from, whether or not that run finished.

    Attributes:
        run_id: The run the session was found in.
        harness: That run's harness.
        home_dir: The session's run home.
        cwd: The working directory the session ran in.
        stack: The stack ref the run used (`"name@version"`).
        resumable: The run's `Spec.resumable`; for a finished run, `RunResult.resumable`.
        cost_before: The session's cumulative cost so far (the baseline for harnesses that report
            cumulative cost); `0.0` for a run that never finished.
        finished: `True` when the run wrote `result.json`; `False` for a run that crashed.
    """

    run_id: str
    harness: str
    home_dir: str
    cwd: str
    stack: str
    resumable: bool
    cost_before: float
    finished: bool

    @classmethod
    def from_result(cls, r: RunResult) -> "SessionRef":
        """A finished run: the baseline is its `session_cost_usd`, else (an older record) its `cost_usd`."""
        cost = r.session_cost_usd if r.session_cost_usd is not None else (r.cost_usd or 0.0)
        return cls(run_id=r.run_id, harness=r.harness, home_dir=r.home_dir, cwd=r.cwd, stack=r.stack,
                   resumable=r.resumable, cost_before=cost, finished=True)

    @classmethod
    def from_record(cls, run_id: str, record: dict) -> "SessionRef":
        """A run known only from its `session.json`. Raises `KeyError`/`TypeError` on a malformed record."""
        texts = {k: record[k] for k in ("harness", "home", "cwd", "stack")}
        if not all(isinstance(v, str) for v in texts.values()) or not isinstance(record["resumable"], bool):
            raise TypeError("malformed session record")
        return cls(run_id=run_id, harness=texts["harness"], home_dir=texts["home"], cwd=texts["cwd"],
                   stack=texts["stack"], resumable=record["resumable"], cost_before=0.0, finished=False)
