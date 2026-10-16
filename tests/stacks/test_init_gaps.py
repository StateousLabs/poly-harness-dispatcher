import json
import shutil

import pytest

import phd
from conftest import FIXTURES
from phd.errors import StackInvalid
from phd.stacks import _as_manifest


@pytest.fixture
def store(tmp_path):
    return tmp_path / "store"


def test_prune_via_the_public_api_drops_index_entries_with_missing_folders(store):
    stack = phd.stacks.resolve(FIXTURES / "stack", store_root=store)
    shutil.rmtree(stack.root.parent)
    phd.stacks.prune(store_root=store)
    assert phd.stacks.list(store_root=store) == []
    assert json.loads((store / "index.json").read_text(encoding="utf-8")) == {}


def test_as_manifest_reads_a_manifest_file_directly(tmp_path):
    path = tmp_path / "my.toml"
    path.write_text('name = "m"\nversion = "1"\n', encoding="utf-8")
    m = _as_manifest(path)
    assert m.ref == "m@1" and m.path == path.resolve()


def test_as_manifest_reads_a_folder_with_stack_toml(tmp_path):
    (tmp_path / "stack.toml").write_text('name = "f"\nversion = "2"\n', encoding="utf-8")
    m = _as_manifest(tmp_path)
    assert m.ref == "f@2" and m.path == (tmp_path / "stack.toml").resolve()


def test_as_manifest_wraps_a_plain_config_folder():
    m = _as_manifest(FIXTURES / "stack")
    assert m.base is not None and m.base.source == str((FIXTURES / "stack").resolve()) and m.path is None


def test_as_manifest_rejects_an_unrecognised_folder(tmp_path):
    with pytest.raises(StackInvalid, match="not a stack.toml, a folder with stack.toml, or a config/ folder"):
        _as_manifest(tmp_path)
