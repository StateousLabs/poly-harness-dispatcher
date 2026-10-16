"""Small materialize branches: hook matchers, instruction files, and the shim's __main__ guard."""

import runpy
import sys
from pathlib import Path

import pytest

from phd.materialize import hooks
from phd.materialize.hooks import shim as hook_shim
from phd.materialize.prompt import assemble
from phd.spec import Inputs


def test_hook_without_a_tool_gets_no_matcher(tmp_path):
    shim = tmp_path / "home" / "phd" / "hook_shim.py"
    block = hooks.claude_style_block("claude", [{"event": "post_tool", "tool": None, "command": "c", "timeout_s": 5.0}], shim)
    (entry,) = block["PostToolUse"]
    assert "matcher" not in entry


def test_instructions_only_skips_absent_project_files(stack, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("ONLY-AGENTS", encoding="utf-8")
    p = assemble(stack.identity, Inputs(msg="hi"), cwd, "instructions_only")
    assert "## Project context (AGENTS.md)" in p.system and "ONLY-AGENTS" in p.system
    assert "CLAUDE.md" not in p.system


def test_shim_module_exits_with_main_status_when_run_as_a_script(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hook_shim.py"])  # no harness/index: main returns 2
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(hook_shim.__file__)), run_name="__main__")
    assert exc.value.code == 2
