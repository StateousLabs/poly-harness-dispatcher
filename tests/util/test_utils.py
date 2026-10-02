import os
import sys
import tomllib
from pathlib import Path

import pytest

from phd._util import fs as _fs, shell as _shell, toml as _toml
from phd.errors import HarnessNotInstalled


def test_deep_merge_unions_lists_and_keeps_base_on_conflict():
    conflicts: list[str] = []
    base = {"permissions": {"allow": ["Skill"]}, "model": "phd"}
    extra = {"permissions": {"allow": ["Skill", "Bash(echo:*)"], "deny": ["Read"]}, "model": "user", "new": 1}
    out = _fs.deep_merge(base, extra, conflicts=conflicts)
    assert out == {"permissions": {"allow": ["Skill", "Bash(echo:*)"], "deny": ["Read"]}, "model": "phd", "new": 1}
    assert conflicts == ["model"]
    assert base == {"permissions": {"allow": ["Skill"]}, "model": "phd"}  # not mutated


def test_is_within(tmp_path):
    assert _fs.is_within(tmp_path / "a" / "b", tmp_path)
    assert not _fs.is_within(tmp_path.parent, tmp_path)


def test_robust_rmtree_removes_read_only_files(tmp_path):
    d = tmp_path / "home"
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "ro.txt"
    f.write_text("x")
    f.chmod(0o444)
    _fs.robust_rmtree(d)
    assert not d.exists()
    _fs.robust_rmtree(d)  # already gone: no error


def test_toml_round_trips_through_tomllib():
    data = {
        "model": "gpt-5.6-terra",
        "approval_policy": "never",
        "project_doc_max_bytes": 0,
        "mcp_servers": {
            "echo": {"command": "python", "args": ["C:\\stacks\\mcp\\echo.py"],
                     "env": {"ECHO_PREFIX": "phd-echo"}, "default_tools_approval_mode": "approve"},
            "needs quoting": {"url": "http://x", "http_headers": {"Authorization": "Bearer t"}},
        },
        "skills": {"config": [{"path": "C:/Users/u/.agents/skills/a/SKILL.md", "enabled": False},
                              {"path": "café/SKILL.md", "enabled": False}]},
        "flags": [],
        "ratio": 0.5,
    }
    assert tomllib.loads(_toml.dumps(data)) == data


def test_shell_join_quotes_only_when_needed():
    assert _shell.shell_join(["C:/Python313/python.exe", "C:/run/home/phd/hook_shim.py", "codex", "0"]) == \
        "C:/Python313/python.exe C:/run/home/phd/hook_shim.py codex 0"
    assert _shell.shell_join(["C:/Program Files/py/python.exe", "x"]) == '"C:/Program Files/py/python.exe" x'


def test_resolve_argv_absolute_executable():
    assert _shell.resolve_argv([sys.executable, "-V"]) == [sys.executable, "-V"]


def test_resolve_argv_missing_executable():
    with pytest.raises(HarnessNotInstalled, match="definitely-not-a-harness-xyz"):
        _shell.resolve_argv(["definitely-not-a-harness-xyz", "-p"])


@pytest.mark.skipif(os.name != "nt", reason="Windows .cmd shims only")
def test_resolve_argv_wraps_cmd_shims(tmp_path):
    shim = tmp_path / "fake.cmd"
    shim.write_text("@echo off\r\n")
    argv = _shell.resolve_argv([str(shim), "-p"])
    assert argv[1:] == ["/c", str(shim), "-p"]
    assert argv[0].lower().endswith("cmd.exe")


def test_write_private_writes_exact_text_and_mode(tmp_path):
    creds_path = tmp_path / "nested" / "secret.txt"
    text_with_unicode = "token=xyz&café=1\n"

    # Write new file
    _fs.write_private(creds_path, text_with_unicode)
    assert creds_path.read_text(encoding="utf-8") == text_with_unicode

    # Check mode is 0o600 on POSIX (Windows ignores POSIX modes)
    if os.name != "nt":
        mode = creds_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    # Overwrite existing file with new text
    new_text = "new_secret=abc\ncafé2"
    _fs.write_private(creds_path, new_text)
    assert creds_path.read_text(encoding="utf-8") == new_text


def test_write_json_creates_parents_and_ensures_unicode(tmp_path):
    json_path = tmp_path / "a" / "b" / "c" / "data.json"
    data = {"name": "café", "value": 42, "flag": True}

    _fs.write_json(json_path, data)

    assert json_path.exists()
    import json as json_lib
    loaded = json_lib.loads(json_path.read_text(encoding="utf-8"))
    assert loaded == data
    # Verify unicode was preserved (not escaped)
    content = json_path.read_text(encoding="utf-8")
    assert "café" in content


def test_remove_path_read_only_file(tmp_path):
    import stat
    f = tmp_path / "ro.json"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, stat.S_IREAD)
    _fs.remove_path(f)
    assert not f.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_remove_path_removes_a_junction_not_its_target(tmp_path):
    import _winapi
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "home" / "skills"
    link.parent.mkdir()
    _winapi.CreateJunction(str(target), str(link))
    assert link.is_junction()
    _fs.remove_path(link)
    assert not os.path.lexists(link) and (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_path_handles_files_dirs_and_missing(tmp_path):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "sub" / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    for name in ("d", "f.txt", "missing"):
        _fs.remove_path(tmp_path / name)
    assert list(tmp_path.iterdir()) == []
