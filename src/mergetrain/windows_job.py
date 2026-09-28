"""Windows Job Objects: stop a process together with everything it started.

``taskkill /T`` follows parent links, so it misses a descendant whose parent
already exited, which MSYS programs such as Git for Windows' ``sh`` do
routinely (#215). A process that starts suspended and joins a job before it
runs cannot start anything outside that job, and terminating the job reaches
every process in it, including those in jobs nested below it.

The job deliberately does not allow breakaway. Cygwin-based programs, which
include Git for Windows' ``sh`` and ``sleep``, ask for CREATE_BREAKAWAY_FROM_JOB
whenever their job permits it, so a job that allowed it would lose exactly the
processes it exists to stop.

This module imports nothing from mergetrain, so the thin MCP adapter may use it.
"""

from __future__ import annotations

import functools
import sys
import time
from types import SimpleNamespace

# CreateProcess flag: start the process with its main thread suspended.
CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_MAX_LISTED_PROCESSES = 4096
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000


@functools.cache
def _api() -> SimpleNamespace:  # pragma: no cover - Windows only
    """Load the kernel32 and ntdll calls that job control needs."""

    if sys.platform != "win32":
        raise OSError("Windows job objects exist only on Windows")
    import ctypes
    from ctypes import wintypes

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

    class ProcessIdList(ctypes.Structure):
        _fields_ = [
            ("assigned", wintypes.DWORD),
            ("listed", wintypes.DWORD),
            ("ids", ctypes.c_size_t * _MAX_LISTED_PROCESSES),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    ntdll = ctypes.WinDLL("ntdll")
    handle, flag, dword = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
    for function, argtypes, restype in (
        (kernel32.CreateJobObjectW, (wintypes.LPVOID, wintypes.LPCWSTR), handle),
        (kernel32.OpenJobObjectW, (dword, flag, wintypes.LPCWSTR), handle),
        (
            kernel32.QueryInformationJobObject,
            (handle, ctypes.c_int, wintypes.LPVOID, dword, wintypes.LPDWORD),
            flag,
        ),
        (kernel32.OpenProcess, (dword, flag, dword), handle),
        (kernel32.AssignProcessToJobObject, (handle, handle), flag),
        (kernel32.IsProcessInJob, (handle, handle, ctypes.POINTER(flag)), flag),
        (kernel32.TerminateJobObject, (handle, wintypes.UINT), flag),
        (kernel32.WaitForSingleObject, (handle, dword), dword),
        (kernel32.CloseHandle, (handle,), flag),
        (ntdll.NtResumeProcess, (handle,), ctypes.c_long),
    ):
        function.argtypes = argtypes
        function.restype = restype
    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        kernel32=kernel32,
        ntdll=ntdll,
        Accounting=Accounting,
        ProcessIdList=ProcessIdList,
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
    def create(cls, name: str = "") -> WindowsJob | None:
        """Return an empty job, or None when Windows does not provide one.

        A named job outlives this process for as long as a process in it is
        alive, so another process can ask ``named_job_active`` about it.
        """

        try:
            api = _api()
        except (AttributeError, OSError):
            return None
        handle = api.kernel32.CreateJobObjectW(None, name or None)
        if not handle:
            return None
        return cls(api, handle)

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

        TerminateJobObject only starts the kill, and a process leaves the job's
        count before Windows releases its address space and mapped images.
        Callers go on to delete those files, so wait on each process itself
        until it has exited, or until ``timeout`` expires first.
        """

        if self._handle is None:
            return False
        kernel32 = self._api.kernel32
        members = self._open_members()
        try:
            if not kernel32.TerminateJobObject(self._handle, 1):
                return False
            deadline = time.monotonic() + timeout
            for member in members:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                kernel32.WaitForSingleObject(member, remaining_ms)
            # The job still counts a process that started after the snapshot.
            while self._active_processes() and time.monotonic() < deadline:
                time.sleep(0.01)
            return True
        finally:
            for member in members:
                kernel32.CloseHandle(member)

    def _open_members(self) -> list[int]:
        """Open a waitable handle to each process now in the job."""

        api = self._api
        listing = api.ProcessIdList()
        if not api.kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            api.ctypes.byref(listing),
            api.ctypes.sizeof(listing),
            None,
        ):
            return []
        members: list[int] = []
        for pid in listing.ids[: listing.listed]:
            handle = api.kernel32.OpenProcess(
                _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                continue
            # The process may have exited and its id been reused since the listing.
            in_job = api.wintypes.BOOL()
            if (
                api.kernel32.IsProcessInJob(handle, self._handle, api.ctypes.byref(in_job))
                and in_job.value
            ):
                members.append(handle)
            else:
                api.kernel32.CloseHandle(handle)
        return members

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


def named_job_active(name: str) -> bool:  # pragma: no cover - Windows only
    """Whether the named job still holds a running process.

    A job lives on after its creator exits while any process in it runs, so
    this still sees a command whose parent was killed. Any failure to open or
    query the job reads as inactive, which is what a missing job means.
    """

    try:
        api = _api()
    except (AttributeError, OSError):
        return False
    handle = api.kernel32.OpenJobObjectW(_JOB_OBJECT_QUERY, False, name)
    if not handle:
        return False
    try:
        return WindowsJob(api, handle)._active_processes() > 0
    finally:
        api.kernel32.CloseHandle(handle)
