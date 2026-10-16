"""LocalLedger / SessionRef / phd.runs edges not covered by test_ledger.py."""

import pytest

from phd import runs
from phd.results import LocalLedger
from phd.results import ledger as ledger_mod
from phd.results.session import SessionRef
from test_ledger import result


def _session(**over):
    rec = {"session_id": "sid", "started_at": "2026-10-01T10:00:00+00:00", "harness": "claude",
           "home": "/h", "cwd": "/w", "stack": "s@1", "resumable": True}
    rec.update(over)
    return rec


def _write_session(ledger, rec):
    rid, d = ledger.allocate()
    ledger.write_session(d, rec)
    return rid, d


def test_allocate_retries_when_the_run_id_is_taken(tmp_path, monkeypatch):
    ledger = LocalLedger(tmp_path)
    (tmp_path / "taken").mkdir()
    ids = iter(["taken", "fresh"])
    monkeypatch.setattr(ledger_mod, "new_run_id", lambda: next(ids))
    run_id, run_dir = ledger.allocate()
    assert run_id == "fresh" and run_dir == ledger.root / "fresh" and run_dir.is_dir()


async def test_list_filters_by_stack(tmp_path):
    ledger = LocalLedger(tmp_path)
    for rid, stack in (("a", "s@1"), ("b", "other@2")):
        _, d = ledger.allocate()
        r = result(d, rid)
        r.stack = stack
        await ledger.put(r)
    assert [r.run_id for r in ledger.list(stack="other@2")] == ["b"]
    assert ledger.list(stack="none@0") == []


def test_find_session_skips_records_with_non_string_started_at(tmp_path):
    ledger = LocalLedger(tmp_path)
    _write_session(ledger, _session(started_at=123))
    assert ledger.find_session("sid") is None


def test_find_session_skips_malformed_and_other_sessions(tmp_path):
    ledger = LocalLedger(tmp_path)
    _write_session(ledger, _session(session_id="other"))
    _write_session(ledger, _session(resumable="yes"))
    rid, _ = _write_session(ledger, _session(started_at="2026-10-02T10:00:00+00:00"))
    ref = ledger.find_session("sid")
    assert ref.run_id == rid and ref.finished is False and ref.cost_before == 0.0


async def test_find_session_defers_to_a_run_that_finished_after_all(tmp_path):
    # session.json says "sid", but the run's result.json (the authority) records a different session
    ledger = LocalLedger(tmp_path)
    rid, d = _write_session(ledger, _session())
    r = result(d, rid)
    r.session_id = "different"
    await ledger.put(r)
    assert ledger.find_session("sid") is None


def test_session_ref_from_record_rejects_malformed_records():
    with pytest.raises(TypeError, match="malformed session record"):
        SessionRef.from_record("r1", _session(cwd=None))
    with pytest.raises(KeyError):
        SessionRef.from_record("r1", {"harness": "claude"})
    ref = SessionRef.from_record("r1", _session())
    assert (ref.harness, ref.home_dir, ref.cwd, ref.stack, ref.resumable) == ("claude", "/h", "/w", "s@1", True)


async def test_runs_module_reads_the_default_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_mod, "DEFAULT_RUNS_ROOT", tmp_path)
    ledger = LocalLedger()
    rid, d = ledger.allocate()
    await ledger.put(result(d, rid, harness="codex"))
    assert [r.run_id for r in runs.list(harness="codex")] == [rid]
    assert runs.list(harness="claude") == []
    assert runs.get(rid).harness == "codex"
    with pytest.raises(KeyError):
        runs.get("missing")
