"""PHD neutral-hook shim. Copied into every run home and invoked by the harness as

    python hook_shim.py <claude|codex|neutral> <hook_index>

It translates the harness's hook payload into the neutral PHD event (CoreDesign §10), runs the
stack's hook command with that event on stdin, and relays the result: exit 2 blocks, 0 allows.
Standard library only — it runs under the host's Python, outside the phd package."""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAUDE_TO_NEUTRAL = {"Bash": "shell", "Read": "read", "Write": "write", "Edit": "edit",
                     "MultiEdit": "edit", "WebFetch": "web_fetch", "WebSearch": "web_search"}


def neutral_tool(name: str) -> str:
    if name.startswith("mcp__"):
        _, server, tool = name.split("__", 2)
        return f"mcp:{server}/{tool}"
    return CLAUDE_TO_NEUTRAL.get(name, name)


def to_neutral(harness: str, event: str, raw: dict) -> dict:
    if harness in ("claude", "codex"):  # codex sends Claude-compatible payloads (verified 2026-10-01)
        tool = raw.get("tool_name")
        return {"event": event, "harness": harness, "tool": neutral_tool(tool) if tool else None,
                "input": raw.get("tool_input"), "output": raw.get("tool_response"), "cwd": raw.get("cwd")}
    return raw  # the opencode plugin already sends the neutral shape


def main(argv: list[str]) -> int:
    harness, index = argv[1], int(argv[2])
    hook = json.loads((HERE / "hooks.resolved.json").read_text(encoding="utf-8"))[index]
    config = json.loads((HERE / "shim_config.json").read_text(encoding="utf-8"))
    raw = json.loads(sys.stdin.read() or "{}")
    event = to_neutral(harness, hook["event"], raw)
    event["run_id"] = config["run_id"]
    env = dict(os.environ, PHD_RUN_DIR=config["run_dir"], PHD_RUN_ID=config["run_id"])
    timeout = hook.get("timeout_s", 30)
    try:
        proc = subprocess.run(hook["command"], shell=True, input=json.dumps(event), text=True,
                              capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"PHD hook timed out after {timeout}s: {hook['command']}\n")
        return 1
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return {0: 0, 2: 2}.get(proc.returncode, 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
