import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from conftest import FIXTURES
from phd.errors import StackConflict, StackInvalid, StackNotFound
from phd.stacks.store import STAGING_MAX_AGE_S, StackStore, identity_hash, store_root


def stage(store, name="s", version="1", tweak=None):
    """A staged dir holding config/ (a copy of the fixture stack with name/version set)."""
    staged = store.new_staging()
    config = staged / "config"
    shutil.copytree(FIXTURES / "stack", config)
    meta = json.loads((config / "stack.json").read_text())
    meta.update(name=name, version=version)
    (config / "stack.json").write_text(json.dumps(meta))
    if tweak:
        (config / "agents" / "helper.md").write_text(tweak)
    (staged / "stack.lock.json").write_text("{}")
    return staged, identity_hash(config)


def put(store, staged, ident, name="s", version="1", **kw):
    kw.setdefault("allow_duplicates", False)
    kw.setdefault("replace", False)
    return store.put(staged, name=name, version=version, ident=ident, manifest=None, **kw)


def test_store_root_default_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PHD_STACKS_ROOT", str(tmp_path / "env-root"))
    assert store_root() == (tmp_path / "env-root").resolve()
    assert store_root(tmp_path / "x") == (tmp_path / "x").resolve()


def test_staging_lives_under_the_store_root(tmp_path):  # Review Focus 4
    store = StackStore(tmp_path / "store")
    assert store.new_staging().is_relative_to(store.root)


def test_new_entry(tmp_path):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    e = put(store, staged, ident)
    assert e.ref == "s@1" and not e.reused and e.path == store.root / "s" / "1" / "config"
    assert (e.path / "stack.json").is_file() and (e.path.parent / "stack.lock.json").is_file()
    assert json.loads((store.root / "index.json").read_text())["s@1"]["hash"] == ident
    assert not staged.exists()


def test_same_ref_same_content_is_reused(tmp_path):  # Review Focus 3
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    staged, ident = stage(store)
    e = put(store, staged, ident)
    assert e.ref == "s@1" and not e.reused
    assert [x.ref for x in store.list()] == ["s@1"]


def test_identical_content_under_other_ref(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store, "a", "1"), name="a", version="1")
    staged, ident = stage(store, "b", "1")
    e = put(store, staged, ident, name="b", version="1")
    assert e.ref == "a@1" and e.requested == "b@1" and e.reused
    assert [x.ref for x in store.list()] == ["a@1"]
    staged, ident = stage(store, "b", "1")
    e = put(store, staged, ident, name="b", version="1", allow_duplicates=True)
    assert e.ref == "b@1" and not e.reused
    assert sorted(x.ref for x in store.list()) == ["a@1", "b@1"]


def test_conflict_and_replace(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    staged, ident = stage(store, tweak="# changed\n")
    with pytest.raises(StackConflict, match="s@1 is already stored with different content"):
        put(store, staged, ident)
    staged, ident = stage(store, tweak="# changed\n")
    e = put(store, staged, ident, replace=True)
    assert (e.path / "agents" / "helper.md").read_text() == "# changed\n"
    assert json.loads((store.root / "index.json").read_text())["s@1"]["hash"] == ident


def test_new_version_is_stored_alongside(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store, "s", "1"))
    put(store, *stage(store, "s", "2", tweak="# v2\n"), name="s", version="2")
    assert sorted(x.ref for x in store.list()) == ["s@1", "s@2"]
    assert store.get("s").ref == "s@2" and store.get("s@1").version == "1"


def test_get_remove_prune(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    with pytest.raises(StackNotFound):
        store.get("nope")
    with pytest.raises(StackNotFound):
        store.remove("s@1", "nope@1")
    assert store.get("s@1")  # nothing removed when one ref is unknown
    leftover = _aged(store, owner=None)
    store.remove("s@1")
    assert not (store.root / "s" / "1").exists() and store.list() == []
    store.prune()
    assert not leftover.exists()


def test_identity_ignores_name_version_description(tmp_path):
    store = StackStore(tmp_path / "store")
    a, ia = stage(store, "a", "1")
    b, ib = stage(store, "b", "9")
    assert ia == ib


WORKER = r'''
import json, shutil, sys
from phd.stacks.store import StackStore, identity_hash
root, fixture, name = sys.argv[1:4]
store = StackStore(root)
staged = store.new_staging()
config = staged / "config"
shutil.copytree(fixture, config)
meta = json.loads((config / "stack.json").read_text())
meta.update(name=name, version="1")
(config / "stack.json").write_text(json.dumps(meta))
(staged / "stack.lock.json").write_text("{}")
print(store.put(staged, name=name, version="1", ident=identity_hash(config), manifest=None,
                allow_duplicates=True, replace=False).ref)
'''


def test_concurrent_puts_keep_a_valid_index(tmp_path):
    # separate interpreters (subprocess, not multiprocessing: test modules are not importable in a spawned child)
    root = tmp_path / "store"
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(root), str(FIXTURES / "stack"), f"n{i}"],
                              stdout=subprocess.PIPE, text=True) for i in range(4)]
    refs = sorted(p.communicate(timeout=120)[0].strip() for p in procs)
    assert refs == ["n0@1", "n1@1", "n2@1", "n3@1"]
    assert sorted(json.loads((root / "index.json").read_text())) == refs


def _index(store):
    return json.loads((store.root / "index.json").read_text())


def test_write_index_and_move_retry_sharing_violations(tmp_path, monkeypatch):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    real, left = os.replace, {"n": 4}

    def flaky(*a, **kw):
        if left["n"] > 0:
            left["n"] -= 1
            raise PermissionError("sharing violation")
        return real(*a, **kw)
    monkeypatch.setattr(os, "replace", flaky)
    assert put(store, staged, ident).ref == "s@1" and left["n"] == 0
    assert _index(store)["s@1"]["hash"] == ident


def test_put_rejects_unsafe_name_or_version(tmp_path):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    for name, version in [("..", "1"), ("s", ".."), ("a/b", "1"), ("s", "1/../.."), ("", "1"), ("-x", "1")]:
        with pytest.raises(StackInvalid):
            put(store, staged, ident, name=name, version=version)
    assert staged.exists()


def test_remove_refuses_tampered_index_entries(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    index = _index(store)
    index["..@.."] = {"hash": "h", "created_at": "2020"}
    index[f"{outside}@1"] = {"hash": "h", "created_at": "2020"}
    (store.root / "index.json").write_text(json.dumps(index))
    for ref in ("..@..", f"{outside}@1"):
        with pytest.raises(StackInvalid):
            store.remove("s@1", ref)
    with pytest.raises(StackInvalid):
        store.prune()
    assert (outside / "keep.txt").exists() and store.get("s@1")


def test_replace_writes_the_index_before_deleting_the_old_folder(tmp_path, monkeypatch):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    staged, ident = stage(store, tweak="# changed\n")

    def boom(path):
        raise OSError("locked")
    monkeypatch.setattr("phd.stacks.store.robust_rmtree", boom)
    put(store, staged, ident, replace=True)
    monkeypatch.undo()
    assert _index(store)["s@1"]["hash"] == ident
    staged, ident2 = stage(store, tweak="# changed\n")
    assert ident2 == ident and not put(store, staged, ident2).reused


def test_old_rule_index_hash_is_refreshed_not_a_conflict(tmp_path):
    # hashes computed before dot-folders were covered: re-resolving the identical stack must not conflict
    store = StackStore(tmp_path / "store")

    def stage_dot(name="s"):
        staged, _ = stage(store, name=name)
        (staged / "config" / ".extra").mkdir()
        (staged / "config" / ".extra" / "f.txt").write_text("x")
        return staged, identity_hash(staged / "config")

    staged, ident = stage_dot()
    first = put(store, staged, ident)
    index = _index(store)
    index["s@1"]["hash"] = "sha256:oldrule"
    (store.root / "index.json").write_text(json.dumps(index))
    staged, ident = stage_dot()
    again = put(store, staged, ident)
    assert again.hash == ident and not staged.exists() and again.created_at == first.created_at
    assert _index(store)["s@1"]["hash"] == ident and _index(store)["s@1"]["created_at"] == first.created_at
    # a real content difference with a stale index hash is still a conflict
    index = _index(store)
    index["s@1"]["hash"] = "sha256:oldrule"
    (store.root / "index.json").write_text(json.dumps(index))
    staged, ident = stage(store, tweak="# changed" + chr(10))
    with pytest.raises(StackConflict):
        put(store, staged, ident)


def test_prune_keeps_fresh_staging_and_removes_aged(tmp_path):
    store = StackStore(tmp_path / "store")
    fresh, aged = store.new_staging(), _aged(store, owner=None)
    store.prune()
    assert fresh.exists() and not aged.exists()


def _dead_pid() -> int:
    out = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(out.stdout)


def _aged(store, *, owner, age_s=2 * STAGING_MAX_AGE_S):
    """A staging folder `age_s` old whose owner is `owner` (a pid), or has no owner file when None."""
    path = store.new_staging()
    if owner is None:
        (path / ".owner.json").unlink(missing_ok=True)
    else:
        (path / ".owner.json").write_text(json.dumps({"pid": owner, "started_at": "2026-01-01T00:00:00+00:00"}))
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_new_staging_records_its_owner(tmp_path):
    store = StackStore(tmp_path / "store")
    owner = json.loads((store.new_staging() / ".owner.json").read_text())
    assert owner["pid"] == os.getpid() and owner["started_at"]


def test_prune_keeps_an_old_staging_folder_whose_owner_is_alive(tmp_path):
    store = StackStore(tmp_path / "store")
    mine = _aged(store, owner=os.getpid())  # this process: a slow resolve still running
    store.prune()
    assert mine.exists()


def test_prune_removes_old_staging_folders_of_dead_or_unknown_owners(tmp_path):
    store = StackStore(tmp_path / "store")
    dead, ownerless = _aged(store, owner=_dead_pid()), _aged(store, owner=None)
    garbled = _aged(store, owner=None)
    (garbled / ".owner.json").write_text("{not json")
    replaced = store.root / ".staging" / "replaced-abc"  # from a replace: no owner, age rule
    replaced.mkdir()
    old = time.time() - 2 * STAGING_MAX_AGE_S
    for path in (garbled, replaced):  # writing the file above refreshed the folder's mtime
        os.utime(path, (old, old))
    store.prune()
    assert not any(p.exists() for p in (dead, ownerless, garbled, replaced))


def test_prune_keeps_a_young_staging_folder_of_a_dead_owner(tmp_path):
    store = StackStore(tmp_path / "store")
    young = _aged(store, owner=_dead_pid(), age_s=60)
    store.prune()
    assert young.exists()


def test_the_owner_file_is_not_stored_with_the_stack(tmp_path):
    store = StackStore(tmp_path / "store")
    e = put(store, *stage(store))
    assert not (e.path.parent / ".owner.json").exists() and (e.path.parent / "stack.lock.json").is_file()
    assert identity_hash(e.path) == e.hash


def test_reuse_and_conflict_discard_the_staged_folder(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    staged, ident = stage(store)
    put(store, staged, ident)
    assert not staged.exists()
    staged, ident = stage(store, tweak="# changed\n")
    with pytest.raises(StackConflict):
        put(store, staged, ident)
    assert not staged.exists()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"s@1": 3}'])
def test_corrupt_index_is_a_stack_invalid(tmp_path, content):
    store = StackStore(tmp_path / "store")
    store.root.mkdir(parents=True)
    (store.root / "index.json").write_text(content)
    with pytest.raises(StackInvalid, match="stack store index is corrupt"):
        store.list()


@pytest.mark.parametrize("bad", ["x.", "CON", "con", "nul.txt", "Com1", "LPT9.log", "aux"])
def test_put_rejects_windows_hostile_names(tmp_path, bad):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    with pytest.raises(StackInvalid):
        put(store, staged, ident, name=bad)
    with pytest.raises(StackInvalid):
        put(store, staged, ident, version=bad)


def test_unindexed_existing_folder_is_refused(tmp_path):
    store = StackStore(tmp_path / "store")
    (store.root / "s" / "1").mkdir(parents=True)
    staged, ident = stage(store)
    with pytest.raises(StackInvalid, match="s@1 exists but is not indexed"):
        put(store, staged, ident)
    assert not staged.exists() and not (store.root / "index.json").exists()


def test_failed_replace_move_restores_the_old_folder(tmp_path, monkeypatch):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    before = _index(store)
    staged, ident = stage(store, tweak="# changed\n")
    real = os.replace

    def failing(src, dst, *a, **kw):
        if str(src) == str(staged):
            raise OSError("disk says no")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError, match="disk says no"):
        put(store, staged, ident, replace=True)
    monkeypatch.undo()
    assert (store.root / "s" / "1" / "config" / "stack.json").is_file()
    assert _index(store) == before


def test_get_of_an_entry_whose_folder_is_missing(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    shutil.rmtree(store.root / "s" / "1" / "config")
    with pytest.raises(StackNotFound, match=r"stack s@1: folder missing from the store \(run prune\)"):
        store.get("s@1")
    with pytest.raises(StackNotFound, match="folder missing"):
        store.get("s")


def test_put_reheals_a_missing_folder_on_the_same_ref_same_hash_path(tmp_path):
    store = StackStore(tmp_path / "store")
    put(store, *stage(store))
    shutil.rmtree(store.root / "s" / "1")
    staged, ident = stage(store)
    e = put(store, staged, ident)
    assert e.ref == "s@1" and (e.path / "stack.json").is_file() and not staged.exists()
    assert store.get("s@1").ref == "s@1"


def test_permission_error_after_retries_is_stack_invalid(tmp_path, monkeypatch):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    monkeypatch.setattr("phd._util.filelock.time.sleep", lambda s: None)

    def denied(*a, **kw):
        raise PermissionError("sharing violation")
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(StackInvalid, match="in use or not writable") as e:
        put(store, staged, ident)
    assert str(store.root) in str(e.value)
