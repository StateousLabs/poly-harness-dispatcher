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
CLAUDE_TO_NEUTRAL = {"Bash": "shell", "PowerShell": "shell", "Read": "read", "Write": "write", "Edit": "edit",
                     "MultiEdit": "edit", "WebFetch": "web_fetch", "WebSearch": "web_search"}


def neutral_tool(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 2:
            return f"mcp:{parts[1]}"
        else:
            return f"mcp:{parts[1]}/{parts[2]}"
    return CLAUDE_TO_NEUTRAL.get(name, name)


def to_neutral(harness: str, event: str, raw: dict) -> dict:
    if harness in ("claude", "codex"):  # codex sends Claude-compatible payloads (verified 2026-10-01)
        tool = raw.get("tool_name")
        return {"event": event, "harness": harness, "tool": neutral_tool(tool) if tool else None,
                "input": raw.get("tool_input"), "output": raw.get("tool_response"), "cwd": raw.get("cwd")}
    return raw  # the opencode plugin already sends the neutral shape


def main(argv: list[str]) -> int:
    event_type = "pre_tool"  # default to pre_tool for error handling (most restrictive)
    try:
        try:
            harness, index = argv[1], int(argv[2])
        except (IndexError, ValueError) as e:
            sys.stderr.write(f"PHD hook shim error: invalid arguments: {e}\n")
            return 2

        try:
            hook = json.loads((HERE / "hooks.resolved.json").read_text(encoding="utf-8"))[index]
        except (FileNotFoundError, json.JSONDecodeError, IndexError, TypeError) as e:
            sys.stderr.write(f"PHD hook shim error: failed to read hooks.resolved.json: {e}\n")
            return 2

        try:
            config = json.loads((HERE / "shim_config.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, TypeError) as e:
            sys.stderr.write(f"PHD hook shim error: failed to read shim_config.json: {e}\n")
            return 2

        # Determine event type for error responses
        event_type = hook.get("event", "pre_tool")

        try:
            raw = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as e:
            error_code = 2 if event_type == "pre_tool" else 1
            sys.stderr.write(f"PHD hook shim error: invalid stdin JSON: {e}\n")
            return error_code

        event = to_neutral(harness, event_type, raw)
        event["run_id"] = config["run_id"]
        env = dict(os.environ, PHD_RUN_DIR=config["run_dir"], PHD_RUN_ID=config["run_id"])
        timeout = hook.get("timeout_s", 30)
        try:
            proc = subprocess.run(hook["command"], shell=True, input=json.dumps(event), text=True,
                                  capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            error_code = 2 if event_type == "pre_tool" else 1
            sys.stderr.write(f"PHD hook shim error: hook timed out after {timeout}s: {hook['command']}\n")
            return error_code
        except Exception as e:
            error_code = 2 if event_type == "pre_tool" else 1
            sys.stderr.write(f"PHD hook shim error: failed to run hook command: {e}\n")
            return error_code

        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        # exit 2 blocks only before a tool runs; elsewhere (e.g. a claude Stop hook) it would keep the agent going
        return {0: 0, 2: 2 if event_type == "pre_tool" else 1}.get(proc.returncode, 1)
    except Exception as e:
        error_code = 2 if event_type == "pre_tool" else 1
        sys.stderr.write(f"PHD hook shim error: {e}\n")
        return error_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
