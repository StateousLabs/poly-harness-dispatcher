import json
import sys

import pytest

from conftest import FAKE_HARNESS, FIXTURES
from helpers import make_tar
import phd
from phd import Dispatcher, Inputs, RunStatus, Spec
from phd._util.filelock import FileLock
from phd.errors import StackConflict, StackFetchError, StackInvalid, StackNotFound

FAKE = [sys.executable, str(FAKE_HARNESS)]


@pytest.fixture
def store(tmp_path):
    return tmp_path / "store"


@pytest.fixture
def extra_skill(tmp_path):
    d = tmp_path / "café parts" / "extra"  # space + non-ASCII on purpose (Review Focus 5)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: extra\ndescription: d\n---\n", encoding="utf-8")
    return d


def test_resolve_manifest_file(tmp_path, store, extra_skill, http_server):
    archived = tmp_path / "archived"
    archived.mkdir()
    (archived / "SKILL.md").write_text("---\nname: archived\n---\n", encoding="utf-8")
    make_tar(archived, http_server.www / "archived-1.0.tar.gz", top="archived")
    manifest = tmp_path / "stack.toml"
    manifest.write_text(f'''
name = "composed"
version = "1.0.0"
base = "{(FIXTURES / "stack").as_posix()}"
[[skills]]
source = "{extra_skill.as_posix()}"
[[skills]]
source = "{http_server.url}/archived-1.0.tar.gz?X-Amz-Signature=topsecret"
''', encoding="utf-8")
    stack = phd.stacks.resolve(manifest, store_root=store)
    assert stack.ref == "composed@1.0.0" and stack.store.ref == "composed@1.0.0"
    assert [s.name for s in stack.skills] == ["archived", "demo-minimal", "demo-notes", "extra"]
    assert stack.root == store / "composed" / "1.0.0" / "config"
    lock = (stack.root.parent / "stack.lock.json").read_text(encoding="utf-8")
    assert "topsecret" not in lock and "archived-1.0.tar.gz" in lock     # Review Focus 1
    assert json.loads(lock)["ref"] == "composed@1.0.0"


def test_resolve_twice_is_idempotent(store, extra_skill):  # Review Focus 3
    m = phd.stacks.compose("c", "1", base=FIXTURES / "stack", skills=[extra_skill])
    a = phd.stacks.resolve(m, store_root=store)
    b = phd.stacks.resolve(m, store_root=store)
    assert a.store.ref == b.store.ref == "c@1" and not b.store.reused
    assert [e.ref for e in phd.stacks.list(store_root=store)] == ["c@1"]


def test_resolve_a_plain_config_folder(store):
    stack = phd.stacks.resolve(FIXTURES / "stack", store_root=store)
    assert stack.ref == "fixture-helper@0.1.0" and stack.store is not None


def test_renamed_copy_reuses_the_stored_stack(store):
    phd.stacks.resolve(phd.stacks.compose("orig", "1", base=FIXTURES / "stack"), store_root=store)
    s = phd.stacks.resolve(phd.stacks.compose("copy", "1", base=FIXTURES / "stack"), store_root=store)
    assert s.store.reused and s.store.ref == "orig@1" and s.store.requested == "copy@1"


def test_failed_resolve_leaves_no_staging(store, tmp_path):
    with pytest.raises(phd.StackFetchError):
        phd.stacks.resolve(phd.stacks.compose("x", "1", base=tmp_path / "missing"), store_root=store)
    assert not any((store / ".staging").iterdir())


def test_get_list_remove(store):
    phd.stacks.resolve(FIXTURES / "stack", store_root=store)
    assert phd.stacks.get("fixture-helper", store_root=store).ref == "fixture-helper@0.1.0"
    phd.stacks.remove("fixture-helper@0.1.0", store_root=store)
    with pytest.raises(StackNotFound):
        phd.stacks.get("fixture-helper@0.1.0", store_root=store)


def test_end_to_end_with_the_spawner(tmp_path, store, workdir, credentials, monkeypatch, extra_skill):
    stack = phd.stacks.resolve(phd.stacks.compose("e2e", "1", base=FIXTURES / "stack", skills=[extra_skill]),
                               store_root=store)
    spec = Spec(harness="claude", model="haiku", stack=stack, cwd=workdir, credentials=credentials["claude"],
                secrets={"ECHO_PREFIX": "x"})
    d = Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, executables={"claude": FAKE})
    plan = d.plan(Inputs(msg="hi"))
    assert (plan.home / "skills" / "extra" / "SKILL.md").is_file()
    monkeypatch.setenv("FAKE_STREAM", str(FIXTURES / "streams" / "claude_ok.jsonl"))
    assert d.spawn(Inputs(msg="hi")).status is RunStatus.SUCCEEDED
    phd.stacks.remove("e2e@1", store_root=store)
    with pytest.raises(StackNotFound, match="no longer exists"):
        d.plan(Inputs(msg="again"))


def test_public_exports():
    for name in ("stacks", "StoreEntry", "StackFetchError", "StackConflict", "StackNotFound"):
        assert name in phd.__all__ and hasattr(phd, name)


def test_url_scheme_case_and_whitespace_are_fetched_and_never_leak(tmp_path, store, http_server):
    for name in ("upper", "spaced"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        make_tar(d, http_server.www / f"{name}.tar.gz", top=name)
    host = http_server.url.removeprefix("http://")
    stack = phd.stacks.resolve(phd.stacks.compose("u", "1", base=FIXTURES / "stack", skills=[
        f"HTTP://{host}/upper.tar.gz?sig=SECRET", f"  http://{host}/spaced.tar.gz?sig=SECRET"]), store_root=store)
    assert {"upper", "spaced"} <= {s.name for s in stack.skills}
    assert "SECRET" not in (stack.root.parent / "stack.lock.json").read_text(encoding="utf-8")
    for bad in (f"HTTP://{host}/missing.tar.gz?sig=SECRET", f"  http://{host}/missing.tar.gz?sig=SECRET"):
        with pytest.raises(StackFetchError) as e:
            phd.stacks.resolve(phd.stacks.compose("u", "2", base=FIXTURES / "stack", skills=[bad]), store_root=store)
        assert "SECRET" not in str(e.value) and "HTTP 404" in str(e.value)


def test_resolve_of_a_url_string_is_refused_without_leaking(store):
    with pytest.raises(StackInvalid, match="base=") as e:
        phd.stacks.resolve("https://host.example/x.tar.gz?sig=SECRET", store_root=store)
    assert "SECRET" not in str(e.value)


def test_conflict_leaves_no_staging(store, extra_skill):
    phd.stacks.resolve(phd.stacks.compose("c", "1", base=FIXTURES / "stack"), store_root=store)
    with pytest.raises(StackConflict):
        phd.stacks.resolve(phd.stacks.compose("c", "1", base=FIXTURES / "stack", skills=[extra_skill]),
                           store_root=store)
    assert not any((store / ".staging").iterdir())


def test_busy_store_lock_is_stack_invalid(store, monkeypatch):
    monkeypatch.setattr("phd.stacks.store.LOCK_TIMEOUT_S", 0.2)
    with FileLock(store / ".store.lock"):
        with pytest.raises(StackInvalid, match="stack store is busy"):
            phd.stacks.resolve(FIXTURES / "stack", store_root=store)


def test_removed_stack_refuses_a_resumed_turn_without_a_run_dir(tmp_path, store, workdir, credentials,
                                                                monkeypatch):
    stack = phd.stacks.resolve(FIXTURES / "stack", store_root=store)
    spec = Spec(harness="claude", model="haiku", stack=stack, cwd=workdir, credentials=credentials["claude"],
                secrets={"ECHO_PREFIX": "x"})
    runs = tmp_path / "runs"
    d = Dispatcher(spec, runs_root=runs, validate_model=False, executables={"claude": FAKE})
    monkeypatch.setenv("FAKE_STREAM", str(FIXTURES / "streams" / "claude_ok.jsonl"))
    result = d.spawn(Inputs(msg="hi"))
    assert result.session_id
    before = sorted(p.name for p in runs.iterdir())
    phd.stacks.remove(stack.ref, store_root=store)
    with pytest.raises(StackNotFound):
        d.spawn(Inputs(msg="again"), session_id=result.session_id)
    assert sorted(p.name for p in runs.iterdir()) == before
