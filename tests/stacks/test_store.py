import json
import shutil
import subprocess
import sys

import pytest

from conftest import FIXTURES
from phd.errors import StackConflict, StackNotFound
from phd.stacks.store import StackStore, identity_hash, store_root


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
    leftover = store.new_staging()
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
