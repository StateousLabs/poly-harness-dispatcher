"""Process helpers with no domain knowledge."""

import os
import signal
import subprocess


_MAX_PID = 2**31 - 1


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running (on Windows: exists and has not exited)."""
    if pid <= 0 or pid > _MAX_PID:  # a corrupt pid (e.g. from a lock file) must not raise OverflowError below
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Linux: True if /proc says the process has exited but not been reaped (state `Z`); `os.kill(pid, 0)` reports
    such a process as alive. False wherever /proc is unavailable (macOS, Windows)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return False
    # "pid (comm) S ..." - comm may contain spaces and parens, so the state follows the LAST ")".
    return stat.rpartition(b")")[2].lstrip()[:1] == b"Z"


def kill_tree(proc) -> None:
    """Kill a `subprocess.Popen` and its descendants. Windows: `taskkill /T /F` (reaches what `cmd.exe /c` started),
    then `kill()`; POSIX: SIGKILL to the process group (start it with `start_new_session=True`)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:  # includes ProcessLookupError and PermissionError: the process is already gone
        pass
