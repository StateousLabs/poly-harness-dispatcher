from datetime import datetime, timedelta, timezone

import pytest

from phd.errors import NotResumable
from phd.ledger import LocalLedger
from phd.result import RunResult, RunStatus, Usage


def result(run_dir, run_id, harness="claude", started="2026-10-01T10:00:00+00:00"):
    return RunResult(run_id=run_id, harness=harness, model="m", stack="s@1", stack_hash="h",
                     status=RunStatus.SUCCEEDED, exit_code=0, error=None, result_text="ok",
                     structured_output=None, cost_usd=0.01, cost_source="harness", usage=Usage(),
                     num_turns=1, duration_ms=10, session_id="sid", resumable=True, resumed_from=None,
                     run_dir=str(run_dir), home_dir=str(run_dir / "home"), cwd="/w", project_config="ignore",
                     autonomy="restricted", unsafe=False, credential_kind="claude_oauth_token",
                     started_at=started, finished_at=started)


async def test_allocate_put_get_list(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    rid1, d1 = ledger.allocate()
    rid2, d2 = ledger.allocate()
    assert rid1 != rid2 and d1.is_dir() and d1.parent == ledger.root
    await ledger.put(result(d1, rid1, started="2026-10-01T10:00:00+00:00"))
    await ledger.put(result(d2, rid2, harness="codex", started="2026-10-01T11:00:00+00:00"))
    assert ledger.get(rid1).result_text == "ok"
    assert [r.run_id for r in ledger.list()] == [rid1, rid2]
    assert [r.run_id for r in ledger.list(harness="codex")] == [rid2]
    since = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)
    assert [r.run_id for r in ledger.list(since=since)] == [rid2]
    assert not list(d1.glob("*.tmp"))


def test_records(tmp_path):
    ledger = LocalLedger(tmp_path)
    rid, d = ledger.allocate()
    ledger.write_record(d, {"argv": ["claude"]})
    assert ledger.read_record(rid) == {"argv": ["claude"]}
    with pytest.raises(NotResumable):
        ledger.read_record("nope")
    with pytest.raises(KeyError):
        ledger.get("nope")


async def test_list_skips_corrupt_results(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    rid, d = ledger.allocate()
    await ledger.put(result(d, rid))
    for name, body in [("bad-json", "{not json"), ("bad-shape", "{}"), ("bad-type", "[]")]:
        _, bad = ledger.allocate()
        (bad / "result.json").write_text(body, encoding="utf-8")
    assert [r.run_id for r in ledger.list()] == [rid]
