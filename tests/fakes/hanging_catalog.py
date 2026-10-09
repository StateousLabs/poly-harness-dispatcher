"""Fake codex/opencode CLI whose catalogue command starts a child process and then hangs.

FAKE_CHILD_PID  file the child's pid is written to (once the child is running)
"""
import os
import subprocess
import sys
import time


def main() -> int:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    if path := os.environ.get("FAKE_CHILD_PID"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(child.pid))
    time.sleep(300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
