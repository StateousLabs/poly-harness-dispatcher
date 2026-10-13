"""Stand-in harness CLI for tests, driven by environment variables.

FAKE_ECHO_FILE      append {"argv","stdin","cwd","env"} as one JSON line
FAKE_CHILD_PIDFILE  spawn a sleeping grandchild process and write its pid here
FAKE_STREAM         path of a JSONL file to print line by line
FAKE_LINE_DELAY     seconds to sleep before each FAKE_STREAM line
FAKE_SLEEP          seconds to sleep after printing (simulates a hang)
FAKE_EXIT           exit code (default 0)
FAKE_NO_STDIN       set to 1 to never read stdin (the writer blocks once the pipe fills)
FAKE_STDERR         text to write to stderr
FAKE_PIDFILE        write this process's pid here at start
FAKE_STREAMS        os.pathsep-separated JSONL paths; invocation N prints the Nth (needs FAKE_STATE)
FAKE_IGNORE_SIGNALS set to 1 to ignore SIGTERM, SIGINT and (Windows) SIGBREAK, so only a hard kill stops it
FAKE_STATE          counter file for FAKE_STREAMS
FAKE_CAPTURE        a file name in $CODEX_HOME; its bytes (hex, or null if absent) go into the echo line as "captured"
"""
import json
import os
import signal
import subprocess
import sys
import time

PREFIXES = ("CLAUDE_", "CODEX_", "OPENCODE_", "ANTHROPIC_", "OPENAI_", "XDG_", "DISABLE_", "PHD_")


def main() -> int:
    if os.environ.get("FAKE_IGNORE_SIGNALS") == "1":
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_IGN)
    if path := os.environ.get("FAKE_PIDFILE"):
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    if (paths := os.environ.get("FAKE_STREAMS")) and (state := os.environ.get("FAKE_STATE")):
        n = 0
        if os.path.exists(state):
            with open(state) as f:
                n = int(f.read() or 0)
        with open(state, "w") as f:
            f.write(str(n + 1))
        choices = paths.split(os.pathsep)
        os.environ["FAKE_STREAM"] = choices[min(n, len(choices) - 1)]
    stdin = "" if os.environ.get("FAKE_NO_STDIN") == "1" else sys.stdin.buffer.read().decode("utf-8")
    if text := os.environ.get("FAKE_STDERR"):
        sys.stderr.write(text)
        sys.stderr.flush()
    if path := os.environ.get("FAKE_ECHO_FILE"):
        with open(path, "a", encoding="utf-8") as f:
            record = {"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
                      "env": {k: v for k, v in os.environ.items() if k.startswith(PREFIXES)}}
            if name := os.environ.get("FAKE_CAPTURE"):
                target = os.path.join(os.environ.get("CODEX_HOME", ""), name)
                record["captured"] = open(target, "rb").read().hex() if os.path.isfile(target) else None
            f.write(json.dumps(record) + "\n")
    if path := os.environ.get("FAKE_CHILD_PIDFILE"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(path, "w") as f:
            f.write(str(child.pid))
    delay = float(os.environ.get("FAKE_LINE_DELAY", "0"))
    if path := os.environ.get("FAKE_STREAM"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                time.sleep(delay)
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
