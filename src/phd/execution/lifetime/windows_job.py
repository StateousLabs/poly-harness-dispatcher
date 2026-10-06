"""Windows Job Object guard: every process in the job dies when PHD's handle closes (also when PHD dies)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def resume(pid: int) -> None:
    """Resume a process created with CREATE_SUSPENDED (ntdll's NtResumeProcess)."""
    k = _kernel32()
    handle = k.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return
    try:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess(handle)
    finally:
        k.CloseHandle(handle)


class JobGuard:
    def __init__(self) -> None:
        self.warning: str | None = None
        self._job = None

    def attach(self, pid: int) -> None:
        try:
            self._job = self._assign(pid)
        except OSError as e:
            self.warning = f"orphan protection unavailable: {e}"
        finally:
            resume(pid)  # created suspended: it must run whether or not the job worked

    def _assign(self, pid: int):
        k = _kernel32()
        job = k.CreateJobObjectW(None, None)  # no security attributes: the handle is not inheritable
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        proc = None
        try:
            if not k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                             ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            proc = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not proc or not k.AssignProcessToJobObject(job, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            k.CloseHandle(job)
            raise
        finally:
            if proc:
                k.CloseHandle(proc)
        return job

    def kill(self) -> bool:
        if self._job is None:
            return False
        return bool(_kernel32().TerminateJobObject(self._job, 1))

    def release(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            _kernel32().CloseHandle(job)  # kill-on-close also ends stray descendants the harness left behind
