import json
import os
import shutil
from pathlib import Path

import pytest

from phd.stacks.store import StackStore
from test_store import _aged, put, stage


def test_removing_one_version_keeps_the_shared_name_folder(tmp_path):
    store = StackStore(tmp_path / "store")
    staged1, ident1 = stage(store, version="1")
    put(store, staged1, ident1, version="1")
    staged2, ident2 = stage(store, version="2", tweak="# changed\n")
    put(store, staged2, ident2, version="2")
    store.remove("s@1")
    assert not (store.root / "s" / "1").exists()
    assert (store.root / "s" / "2" / "config").is_dir()  # the name folder still holds version 2
    assert [e.ref for e in store.list()] == ["s@2"]


def test_prune_without_a_staging_folder_is_a_no_op(tmp_path):
    store = StackStore(tmp_path / "store")
    assert not (store.root / ".staging").exists()
    store.prune()
    assert store.list() == []


def test_prune_skips_a_staging_entry_it_cannot_stat(tmp_path, monkeypatch):
    store = StackStore(tmp_path / "store")
    unreadable, aged = _aged(store, owner=None), _aged(store, owner=None)
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == unreadable:
            raise PermissionError("denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    store.prune()
    monkeypatch.undo()
    assert unreadable.exists() and not aged.exists()


def test_prune_drops_index_entries_whose_folder_is_gone(tmp_path):
    store = StackStore(tmp_path / "store")
    staged, ident = stage(store)
    put(store, staged, ident)
    shutil.rmtree(store.root / "s" / "1")
    store.prune()
    assert json.loads((store.root / "index.json").read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("pid", ["123", True, None, 1.5])
def test_prune_treats_a_non_integer_owner_pid_as_dead(tmp_path, pid):
    store = StackStore(tmp_path / "store")
    old = _aged(store, owner=None)
    (old / ".owner.json").write_text(json.dumps({"pid": pid}), encoding="utf-8")
    os.utime(old, (1, 1))
    store.prune()
    assert not old.exists()
