"""Windows Job Objects: stop a process together with everything it started.

``taskkill /T`` follows parent links, so it misses a descendant whose parent
already exited, which MSYS programs such as Git for Windows' ``sh`` do
routinely (#215). A process that starts suspended and joins a job before it
runs cannot start anything outside that job, and terminating the job reaches
every process in it, including those in jobs nested below it.

This module imports nothing from mergetrain, so the thin MCP adapter may use it.
"""

from __future__ import annotations

import functools
import sys
import time
from types import SimpleNamespace

# CreateProcess flag: start the process with its main thread suspended.
CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800


@functools.cache
def _api() -> SimpleNamespace:  # pragma: no cover - Windows only
    """Load the kernel32 and ntdll calls that job control needs."""

    if sys.platform != "win32":
        raise OSError("Windows job objects exist only on Windows")
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")
        ]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_int64),
            ("per_job_user_time_limit", ctypes.c_int64),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("total_user_time", ctypes.c_int64),
            ("total_kernel_time", ctypes.c_int64),
            ("this_period_total_user_time", ctypes.c_int64),
            ("this_period_total_kernel_time", ctypes.c_int64),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    ntdll = ctypes.WinDLL("ntdll")
    handle, flag, dword = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
    for function, argtypes, restype in (
        (kernel32.CreateJobObjectW, (wintypes.LPVOID, wintypes.LPCWSTR), handle),
        (kernel32.SetInformationJobObject, (handle, ctypes.c_int, wintypes.LPVOID, dword), flag),
        (
            kernel32.QueryInformationJobObject,
            (handle, ctypes.c_int, wintypes.LPVOID, dword, wintypes.LPDWORD),
            flag,
        ),
        (kernel32.OpenProcess, (dword, flag, dword), handle),
        (kernel32.AssignProcessToJobObject, (handle, handle), flag),
        (kernel32.TerminateJobObject, (handle, wintypes.UINT), flag),
        (kernel32.CloseHandle, (handle,), flag),
        (ntdll.NtResumeProcess, (handle,), ctypes.c_long),
    ):
        function.argtypes = argtypes
        function.restype = restype
    return SimpleNamespace(
        ctypes=ctypes,
        kernel32=kernel32,
        ntdll=ntdll,
        Accounting=Accounting,
        ExtendedLimits=ExtendedLimits,
    )


class WindowsJob:  # pragma: no cover - Windows only
    """A Job Object for one command and every process that command starts.

    Closing the job kills nothing, so a command that exits normally behaves
    like a POSIX process group; only ``terminate`` stops the processes.
    """

    def __init__(self, api: SimpleNamespace, handle: int) -> None:
        self._api = api
        self._handle: int | None = handle

    @classmethod
    def create(cls) -> WindowsJob | None:
        """Return an empty job, or None when Windows does not provide one."""

        try:
            api = _api()
        except (AttributeError, OSError):
            return None
        handle = api.kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        job = cls(api, handle)
        limits = api.ExtendedLimits()
        # A program that explicitly breaks away still may, as setsid does on POSIX.
        limits.basic.limit_flags = _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        if not api.kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            api.ctypes.byref(limits),
            api.ctypes.sizeof(limits),
        ):
            job.close()
            return None
        return job

    def adopt(self, pid: int) -> bool:
        """Move a process started with CREATE_SUSPENDED into the job, then resume it.

        Returns whether the process joined the job; it runs either way. Raises
        OSError when the process cannot be resumed: it is then still suspended,
        and the caller must kill it.
        """

        kernel32 = self._api.kernel32
        handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME,
            False,
            pid,
        )
        if not handle:
            raise OSError(f"could not open process {pid}")
        try:
            joined = bool(kernel32.AssignProcessToJobObject(self._handle, handle))
            status = self._api.ntdll.NtResumeProcess(handle)
        finally:
            kernel32.CloseHandle(handle)
        if status < 0:
            raise OSError(f"could not resume process {pid} (NTSTATUS {status & 0xFFFFFFFF:#010x})")
        return joined

    def terminate(self, timeout: float = 10.0) -> bool:
        """Kill every process in the job and in nested jobs, then wait for them to exit.

        TerminateJobObject only starts the kill. Callers go on to delete files
        those processes hold open, so return once the job has no process left,
        or when ``timeout`` expires first.
        """

        if self._handle is None or not self._api.kernel32.TerminateJobObject(self._handle, 1):
            return False
        deadline = time.monotonic() + timeout
        while self._active_processes() and time.monotonic() < deadline:
            time.sleep(0.01)
        return True

    def _active_processes(self) -> int:
        accounting = self._api.Accounting()
        if not self._api.kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._api.ctypes.byref(accounting),
            self._api.ctypes.sizeof(accounting),
            None,
        ):
            return 0
        return int(accounting.active_processes)

    def close(self) -> None:
        if self._handle is not None:
            self._api.kernel32.CloseHandle(self._handle)
            self._handle = None
