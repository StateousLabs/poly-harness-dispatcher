import os
import sys
import tomllib
from pathlib import Path

import pytest

from phd import _fs, _shell, _toml
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
