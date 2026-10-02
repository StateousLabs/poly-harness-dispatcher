"""Neutral hooks (CoreDesign §10) → harness-native hook config.

claude and codex share one schema ({"hooks": {"PreToolUse": [{"matcher", "hooks": [...]}]}});
opencode gets a generated plugin. All of them call the same shim, which hands the stack's hook
command a neutral event."""

import math
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from ..._util.shell import shell_join
from ...errors import HookUnsupported
from ...spec import HookDef

CLAUDE_STYLE_EVENTS = {"session_start": "SessionStart", "pre_tool": "PreToolUse",
                       "post_tool": "PostToolUse", "stop": "Stop"}
OPENCODE_EVENTS = {"pre_tool": "tool.execute.before", "post_tool": "tool.execute.after"}
NATIVE_TOOLS = {
    "claude": {"shell": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
               "web_fetch": "WebFetch", "web_search": "WebSearch"},
    "codex": {"shell": "Bash"},  # verified 2026-10-01; other codex tool names are unverified
    "opencode": {"shell": "bash", "read": "read", "write": "write", "edit": "edit", "web_fetch": "webfetch"},
}

# The harness's hook timer starts before Python even starts, so a native timeout equal to the shim's own
# subprocess timeout lets the harness kill the shim first (its own, possibly fail-open, timeout behaviour).
# The native timeout is padded by this margin so the shim times out first and PHD's fail-closed exit 2 runs.
HOOK_TIMEOUT_MARGIN_S = 10


def native_tool(harness: str, tool: str) -> str:
    if tool.startswith("mcp:") and harness == "claude":
        server, _, name = tool[4:].partition("/")
        return f"mcp__{server}__{name}" if name else f"mcp__{server}__.*"
    try:
        return NATIVE_TOOLS[harness][tool]
    except KeyError:
        raise HookUnsupported(f"{harness}: no native tool name for neutral tool {tool!r}") from None


def check_supported(harness: str, hooks: Iterable[HookDef]) -> None:
    for hook in hooks:
        if harness == "opencode" and hook.event not in OPENCODE_EVENTS:
            raise HookUnsupported(f"opencode: neutral event {hook.event!r} is not supported (only {list(OPENCODE_EVENTS)})")
        if hook.tool is not None:
            native_tool(harness, hook.tool)


def _shim_command(shim: Path, harness: str, index: int) -> str:
    return shell_join([Path(sys.executable).as_posix(), shim.as_posix(), harness, str(index)])


def _is_windows() -> bool:
    return os.name == "nt"


def _hook_command(shim: Path, harness: str, index: int) -> str:
    command = _shim_command(shim, harness, index)
    if harness == "codex" and _is_windows() and command.startswith('"'):
        # Only when the interpreter path needed quoting: cmd.exe /C strips the first and last quote
        # of a line that starts with one, so add one outer pair. An unquoted leading word is left
        # alone (the form verified live against codex).
        return f'"{command}"'
    return command


def claude_style_block(harness: str, hooks: list[dict], shim: Path) -> dict:
    block: dict[str, list] = {}
    for index, hook in enumerate(hooks):
        entry: dict = {"hooks": [{"type": "command", "command": _hook_command(shim, harness, index),
                                  "timeout": int(math.ceil(hook["timeout_s"])) + HOOK_TIMEOUT_MARGIN_S}]}
        if hook.get("tool"):
            matcher = native_tool(harness, hook["tool"])
            # For claude, edit also matches MultiEdit (the shim translates MultiEdit to edit)
            if harness == "claude" and hook["tool"] == "edit":
                matcher = "Edit|MultiEdit"
            # Claude Code on Windows runs shell commands via its PowerShell tool, not Bash
            if harness == "claude" and hook["tool"] == "shell":
                matcher = "Bash|PowerShell"
            entry["matcher"] = matcher
        block.setdefault(CLAUDE_STYLE_EVENTS[hook["event"]], []).append(entry)
    return block
