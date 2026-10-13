"""The codex `auth.json` copy exists in a run home only while a turn runs (item 7)."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

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
        kw.setdefault("resumable", True)
        return make_spec("codex", credentials=Credentials.codex_auth_file(auth_file), **kw)
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
    (root / name / "run.json").write_text("{}", encoding="utf-8")
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


# ---- every cleanup path (review M1)

def lock_files(tmp_path):
    return sorted((tmp_path / "runs").glob("*/home/" + LOCK_NAME))


def failing_write_record(d, monkeypatch, *, delay=0.0):
    real = d.ledger.write_record

    def write_record(run_dir, record):
        if delay:
            time.sleep(delay)
            return real(run_dir, record)
        raise OSError("disk full")
    monkeypatch.setattr(d.ledger, "write_record", write_record)


def test_a_plan_failure_after_materialize_leaves_no_copy(tmp_path, codex_spec, monkeypatch):
    d = dispatcher(codex_spec(), tmp_path)
    failing_write_record(d, monkeypatch)
    with pytest.raises(OSError, match="disk full"):
        d.spawn(Inputs(msg="x"))
    assert leftovers(tmp_path) == [] and lock_files(tmp_path) == []


async def test_a_start_cancelled_while_planning_leaves_no_copy(tmp_path, codex_spec, echo, monkeypatch):
    d = dispatcher(codex_spec(), tmp_path)
    failing_write_record(d, monkeypatch, delay=1.0)
    task = asyncio.ensure_future(d.start(Inputs(msg="x")))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):  # the plan thread finishes, then its cleanup runs in the executor
        if not any((tmp_path / "runs").glob("*/home")):
            break
        await asyncio.sleep(0.1)
    assert leftovers(tmp_path) == [] and not any((tmp_path / "runs").glob("*/home"))


async def _drain(handle):
    return [ev.type async for ev in handle.events()]


async def test_an_interrupted_run_leaves_no_copy(tmp_path, codex_spec, echo, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(codex_spec(), tmp_path).start(Inputs(msg="x"))
    await asyncio.sleep(1)
    handle._task.cancel()  # the pump itself is cancelled: only the finally backstop runs
    events = await asyncio.wait_for(_drain(handle), 10)
    assert "run.finished" not in events and leftovers(tmp_path) == [] and not handle._lock.held


async def test_a_double_cancel_leaves_no_copy(tmp_path, codex_spec, echo, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(codex_spec(), tmp_path).start(Inputs(msg="x"))
    await asyncio.sleep(1)
    first, second = await asyncio.gather(handle.cancel(), handle.cancel())
    assert first is second and first.status is RunStatus.CANCELLED and leftovers(tmp_path) == []


def test_a_resumable_false_run_leaves_no_copy(tmp_path, codex_spec, echo):
    r = dispatcher(codex_spec(resumable=False), tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED and echo()[0]["captured"] is not None
    assert leftovers(tmp_path) == [] and not (tmp_path / "runs" / r.run_id / "home").exists()


def test_a_session_preview_leaves_no_copy(tmp_path, codex_spec, echo):
    d = dispatcher(codex_spec(), tmp_path)
    first = d.spawn(Inputs(msg="x"))
    m = d.plan(Inputs(msg="next"), session_id=first.session_id)
    assert str(m.home) != first.home_dir and (m.home / "config.toml").is_file() and leftovers(tmp_path) == []


# ---- warnings as errors never break cleanup (review I3)

def refuse_deletes(monkeypatch):
    def refuse(path):
        raise PermissionError(13, "in use", str(path))
    monkeypatch.setattr(credential_copies, "_unlink", refuse)


def test_warnings_as_errors_keep_the_real_exception_and_release_the_lock(tmp_path, codex_spec, echo, monkeypatch):
    d = dispatcher(codex_spec(), tmp_path)
    first = d.spawn(Inputs(msg="x"))
    with monkeypatch.context() as patch:
        refuse_deletes(patch)
        failing_write_record(d, patch)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(OSError, match="disk full"):
                d.spawn(Inputs(msg="next"), session_id=first.session_id)
    again = d.spawn(Inputs(msg="again"), session_id=first.session_id)  # the lock was released
    assert again.status is RunStatus.SUCCEEDED and leftovers(tmp_path) == []


async def test_warnings_as_errors_do_not_hang_an_interrupted_run(tmp_path, codex_spec, echo, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    handle = await dispatcher(codex_spec(), tmp_path).start(Inputs(msg="x"))
    await asyncio.sleep(1)
    refuse_deletes(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        handle._task.cancel()
        await asyncio.wait_for(_drain(handle), 10)  # the sentinel still arrives
    assert not handle._lock.held


def test_warnings_as_errors_do_not_fail_the_sweep(tmp_path, codex_spec, monkeypatch):
    dead = leftover_run(tmp_path / "runs", "20260101-dead")
    refuse_deletes(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        m = dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert m.home.is_dir() and (dead / "auth.json").exists() and not (dead / LOCK_NAME).exists()


# ---- the sweep never follows a link (review I1)

def make_link(link, target):
    """A junction on Windows, a symlink elsewhere; skips the test where that is not permitted."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create a link here: {e}")


def victim(tmp_path):
    folder = tmp_path / "victim"  # stands in for a folder like ~/.codex
    folder.mkdir()
    (folder / "auth.json").write_bytes(AUTH)
    return folder


def assert_untouched(folder):
    assert (folder / "auth.json").read_bytes() == AUTH
    assert sorted(p.name for p in folder.iterdir()) == ["auth.json"]  # no lock or takeover file written there


def test_sweep_skips_a_linked_home(tmp_path, codex_spec):
    target = victim(tmp_path)
    run_dir = tmp_path / "runs" / "20260101-link"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    make_link(run_dir / "home", target)
    dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert_untouched(target)


def test_sweep_skips_a_linked_run_directory(tmp_path, codex_spec):
    outside = tmp_path / "outside-run"
    (outside / "home").mkdir(parents=True)
    (outside / "run.json").write_text("{}", encoding="utf-8")
    (outside / "home" / "auth.json").write_bytes(AUTH)
    (tmp_path / "runs").mkdir()
    make_link(tmp_path / "runs" / "20260101-link", outside)
    dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert_untouched(outside / "home")


def test_sweep_skips_a_linked_copy(tmp_path, codex_spec):
    target = victim(tmp_path)
    home = tmp_path / "runs" / "20260101-link" / "home"
    home.mkdir(parents=True)
    (home.parent / "run.json").write_text("{}", encoding="utf-8")
    try:
        os.symlink(target / "auth.json", home / "auth.json")
    except OSError as e:
        pytest.skip(f"cannot create a file symlink here: {e}")
    dispatcher(codex_spec(), tmp_path).plan(Inputs(msg="x"))
    assert_untouched(target)
    assert (home / "auth.json").is_symlink()


# ---- a sweeper in another process delays a resume, it does not fail it (review I2)

@pytest.fixture
def live_helper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    proc.kill()
    proc.wait()


def hold_as_sweeper(home, pid):
    (home / LOCK_NAME).write_text(json.dumps({"pid": pid, "run_id": "phd-sweep-0123abcd", "sweep": True}),
                                  encoding="utf-8")


def test_resume_waits_for_a_sweeper_in_another_process(tmp_path, codex_spec, echo, live_helper):
    d = dispatcher(codex_spec(), tmp_path)
    first = d.spawn(Inputs(msg="x"))
    home = Path(first.home_dir)
    hold_as_sweeper(home, live_helper.pid)
    threading.Timer(0.5, lambda: (home / LOCK_NAME).unlink()).start()  # the sweeper finishes its one delete
    again = d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert again.status is RunStatus.SUCCEEDED and leftovers(tmp_path) == []


def test_a_sweeper_that_never_lets_go_is_busy_after_the_wait(tmp_path, codex_spec, echo, live_helper):
    from phd.errors import NotResumable
    from phd.execution.session_lock import BRIEF_HOLD_WAIT_S
    d = dispatcher(codex_spec(), tmp_path)
    first = d.spawn(Inputs(msg="x"))
    hold_as_sweeper(Path(first.home_dir), live_helper.pid)
    t0 = time.monotonic()
    with pytest.raises(NotResumable) as caught:
        d.spawn(Inputs(msg="next"), session_id=first.session_id)
    assert time.monotonic() - t0 >= BRIEF_HOLD_WAIT_S * 0.9
    assert "busy" in str(caught.value) and "phd-sweep" not in str(caught.value)
