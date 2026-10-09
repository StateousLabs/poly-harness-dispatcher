import json
from datetime import datetime, timezone

import pytest

from phd.errors import NotResumable
from phd.results import LocalLedger
from phd.results import RunResult, RunStatus, Usage


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


async def test_latest_for_session(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    ids = []
    for started, sid in [("2026-10-01T10:00:00+00:00", "sid"), ("2026-10-01T12:00:00+00:00", "sid"),
                         ("2026-10-01T11:00:00+00:00", "sid"), ("2026-10-01T13:00:00+00:00", "other")]:
        rid, d = ledger.allocate()
        r = result(d, rid, started=started)
        r.session_id = sid
        await ledger.put(r)
        ids.append(rid)
    assert ledger.latest_for_session("sid").run_id == ids[1]
    assert ledger.latest_for_session("other").run_id == ids[3]
    assert ledger.latest_for_session("missing") is None


def session_record(ledger, run_id, session_id="sid", started="2026-10-01T10:00:00+00:00", **kw):
    run_dir = ledger.root / run_id
    record = {"session_id": session_id, "harness": "claude", "cwd": "/w", "home": str(run_dir / "home"),
              "stack": "s@1", "resumable": True, "started_at": started} | kw
    (run_dir / "session.json").write_text(json.dumps(record), encoding="utf-8")


async def test_find_session_prefers_the_finished_run(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    done_id, done = ledger.allocate()
    r = result(done, done_id, started="2026-10-01T10:00:00+00:00")
    r.session_cost_usd = 0.5
    await ledger.put(r)
    session_record(ledger, done_id)
    crashed_id, _ = ledger.allocate()  # a later turn that never finished
    session_record(ledger, crashed_id, started="2026-10-01T12:00:00+00:00")
    ref = ledger.find_session("sid")
    assert ref.run_id == done_id and ref.finished and ref.cost_before == 0.5
    assert ref.harness == "claude" and ref.stack == "s@1" and ref.cwd == "/w" and ref.resumable


def test_find_session_falls_back_to_the_newest_unfinished_record(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    ids = []
    for started in ["2026-10-01T10:00:00+00:00", "2026-10-01T12:00:00+00:00", "2026-10-01T11:00:00+00:00"]:
        rid, _ = ledger.allocate()
        session_record(ledger, rid, started=started)
        ids.append(rid)
    other, _ = ledger.allocate()
    session_record(ledger, other, session_id="other", started="2026-10-01T13:00:00+00:00")
    ref = ledger.find_session("sid")
    assert ref.run_id == ids[1] and not ref.finished and ref.cost_before == 0.0
    assert ref.home_dir == str(ledger.root / ids[1] / "home")
    assert ledger.find_session("missing") is None


def test_find_session_skips_corrupt_session_records(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    good, _ = ledger.allocate()
    session_record(ledger, good, started="2026-10-01T10:00:00+00:00")
    for body in ["{not json", "{}", "[]", '{"session_id": "sid"}']:
        rid, d = ledger.allocate()
        (d / "session.json").write_text(body, encoding="utf-8")
    assert ledger.find_session("sid").run_id == good


async def test_find_session_ignores_records_of_finished_runs(tmp_path):
    ledger = LocalLedger(tmp_path / "runs")
    rid, d = ledger.allocate()
    await ledger.put(result(d, rid, harness="codex"))
    session_record(ledger, rid, harness="claude")  # the result wins; the record is not a crashed run
    ref = ledger.find_session("sid")
    assert ref.finished and ref.harness == "codex"
