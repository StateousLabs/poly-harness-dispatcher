"""The codex `auth.json` copy exists in a run home only while a turn runs (item 7)."""

import asyncio
import json
import os
import sys

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus
from phd.execution import credential_copies
from phd.execution.session_lock import LOCK_NAME, SessionLock
from phd.spec import Credentials

FAKE = [sys.executable, str(FAKE_HARNESS)]
AUTH = b'{\n  "auth_mode": "chatgpt",\n  "tokens": {"access_token": "codex-test"}\n}\n'  # LF on purpose


@pytest.fixture
def auth_file(tmp_path):
    path = tmp_path / "source-auth.json"
    path.write_bytes(AUTH)
    return path


@pytest.fixture
def codex_spec(make_spec, auth_file):
    def make(**kw):
        return make_spec("codex", credentials=Credentials.codex_auth_file(auth_file), resumable=True, **kw)
    return make


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    monkeypatch.setenv("FAKE_CAPTURE", "auth.json")
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "codex_ok.jsonl"))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def fresh_sweep_registry():
    credential_copies._SWEPT.clear()
    yield
    credential_copies._SWEPT.clear()


def dispatcher(spec, tmp_path, **kw):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={"codex": kw.pop("executable", FAKE)}, **kw)


def leftovers(tmp_path):
    return sorted((tmp_path / "runs").glob("*/home/auth.json"))


def test_success_leaves_no_copy_and_the_turn_saw_the_exact_bytes(tmp_path, codex_spec, echo):
    r = dispatcher(codex_spec(), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED and r.resumable
    assert bytes.fromhex(echo()[0]["captured"]) == AUTH  # verbatim: LF stays LF on Windows
    assert leftovers(tmp_path) == []


def test_failure_leaves_no_copy(tmp_path, codex_spec, echo, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.delenv("FAKE_STREAM")
    r = dispatcher(codex_spec(), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is not RunStatus.SUCCEEDED and echo()[0]["captured"] is not None
    assert leftovers(tmp_path) == []


async def test_cancel_leaves_no_copy(tmp_path, codex_spec, echo, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(codex_spec(), tmp_path).start(Inputs(msg="x"))
    await asyncio.sleep(1)
    r = await handle.cancel()
    assert r.status is RunStatus.CANCELLED and leftovers(tmp_path) == []


@pytest.mark.parametrize("limit,kind", [({"timeout_s": 2}, "timeout"), ({"idle_timeout_s": 1}, "idle")])
def test_timeout_and_idle_leave_no_copy(tmp_path, codex_spec, echo, monkeypatch, limit, kind):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    r = dispatcher(codex_spec(**limit), tmp_path).spawn(Inputs(msg="x"))
    assert r.error.kind == kind and leftovers(tmp_path) == []


async def test_spawn_failure_leaves_no_copy(tmp_path, codex_spec, echo):
    d = dispatcher(codex_spec(), tmp_path, executable=[str(tmp_path / "no-such-codex.exe")])
    with pytest.raises(OSError):
        await d.start(Inputs(msg="x"))
    assert leftovers(tmp_path) == []


def test_dry_run_leaves_no_copy(tmp_path, codex_spec):
    m = dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert (m.home / "config.toml").is_file() and not (m.home / "auth.json").exists()


def test_resume_recreates_the_copy_for_the_turn_only(tmp_path, codex_spec, echo, auth_file):
    d = dispatcher(codex_spec(), tmp_path)
    first = d.spawn(Inputs(msg="x"))
    rotated = AUTH.replace(b"codex-test", b"rotated")
    auth_file.write_bytes(rotated)
    second = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert second.status is RunStatus.SUCCEEDED and second.home_dir == first.home_dir
    assert bytes.fromhex(echo()[1]["captured"]) == rotated
    assert leftovers(tmp_path) == []


def test_schema_repair_turn_still_has_the_copy(tmp_path, codex_spec, echo):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    r = dispatcher(codex_spec(output_schema=schema, schema_repair=True), tmp_path).spawn(Inputs(msg="x"))
    calls = echo()
    assert "schema repair turn used" in r.warnings and len(calls) == 2
    assert all(c["captured"] is not None for c in calls) and leftovers(tmp_path) == []


def test_a_failed_deletion_does_not_mask_the_result(tmp_path, codex_spec, echo, monkeypatch):
    def refuse(path):
        raise PermissionError(13, "in use", str(path))
    monkeypatch.setattr(credential_copies, "_unlink", refuse)
    r = dispatcher(codex_spec(), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED
    assert any("could not delete the credential copy" in w and "auth.json" in w for w in r.warnings)
    recorded = json.loads((tmp_path / "runs" / r.run_id / "result.json").read_text(encoding="utf-8"))
    assert recorded["warnings"] == r.warnings


def leftover_run(root, name, holder=None):
    home = root / name / "home"
    home.mkdir(parents=True)
    (home / "auth.json").write_bytes(AUTH)
    if holder is not None:
        (home / LOCK_NAME).write_text(json.dumps(holder), encoding="utf-8")
    return home


def test_sweep_deletes_dead_runs_copies_and_keeps_live_ones(tmp_path, codex_spec):
    root = tmp_path / "runs"
    dead = leftover_run(root, "20260101-dead")  # killed before it locked anything, or a lock already released
    stale = leftover_run(root, "20260101-stale", {"pid": 2**31 + 5, "run_id": "gone"})  # pid cannot be alive
    other = leftover_run(root, "20260101-other", {"pid": os.getppid(), "run_id": "elsewhere"})  # live process
    mine = leftover_run(root, "20260101-mine")
    lock = SessionLock(mine, "20260101-mine").acquire("test")  # a run of this process that is still going
    try:
        dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
        assert not (dead / "auth.json").exists() and not (stale / "auth.json").exists()
        assert (other / "auth.json").read_bytes() == AUTH and (mine / "auth.json").read_bytes() == AUTH
        assert json.loads((other / LOCK_NAME).read_text(encoding="utf-8"))["run_id"] == "elsewhere"
        assert not (dead / LOCK_NAME).exists()  # the sweep's own lock is gone again
    finally:
        lock.release()


def test_sweep_runs_once_per_process_per_root(tmp_path, codex_spec):
    root = tmp_path / "runs"
    d = dispatcher(codex_spec(), tmp_path)
    d.plan(Inputs(msg="x"))
    later = leftover_run(root, "20260101-later")
    d.plan(Inputs(msg="x"))
    dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert (later / "auth.json").exists()


def test_sweep_survives_a_missing_root_and_odd_entries(tmp_path, codex_spec):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "stray-file").write_text("x", encoding="utf-8")
    (root / "no-home").mkdir()
    dead = leftover_run(root, "20260101-dead")
    dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert not (dead / "auth.json").exists()


def test_a_fresh_run_holds_its_lock_before_the_copy_is_written(tmp_path, codex_spec, echo, monkeypatch):
    from phd.harnesses.codex.adapter import CodexAdapter
    original, locked = CodexAdapter.credential_env, []

    def checking(self, spec, home):
        locked.append((home / LOCK_NAME).is_file())  # else a concurrent sweep could delete the copy mid-start
        return original(self, spec, home)
    monkeypatch.setattr(CodexAdapter, "credential_env", checking)
    assert dispatcher(codex_spec(), tmp_path).spawn(Inputs(msg="x")).status is RunStatus.SUCCEEDED
    assert locked == [True]
