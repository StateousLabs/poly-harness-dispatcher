"""PHD reaper (POSIX). Standalone: stdlib only, imports nothing from phd.

usage: reaper_main.py <harness pgid>

Reads stdin: a line "exit" means the run ended normally (exit quietly); EOF without it means the
caller died (SIGKILL the harness's process group). Also exits once the group is gone."""

import os
import signal
import sys
import threading


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    pgid = int(sys.argv[1])
    told_exit, eof = threading.Event(), threading.Event()

    def read() -> None:
        for line in sys.stdin:
            if line.strip() == "exit":
                told_exit.set()
                return
        eof.set()

    threading.Thread(target=read, daemon=True).start()
    while not told_exit.is_set():
        if eof.wait(timeout=1.0):
            if told_exit.is_set():
                break
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return 0
        if not _group_alive(pgid):
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
