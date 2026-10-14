"""OS resource caps for child processes: a resource cap, not isolation.

On Windows a Job Object kills the whole tree when its handle closes and can cap
each process's committed memory (allocations beyond it fail, so Python raises
``MemoryError``). On POSIX the worker limits its own address space with
``RLIMIT_AS``. Safety of sandboxed code still comes from interpretation.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from typing import Any, Protocol, cast

__all__ = ["Closeable", "contain", "limit_self"]

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MEMORY_MARGIN = 32 * 2**20


class Closeable(Protocol):
    def close(self) -> None: ...


class _Nothing:
    """POSIX (and fallback) handle: nothing to release."""

    def close(self) -> None:
        return None

    def memory_exceeded(self) -> bool:
        return False


def _structures() -> tuple[type[ctypes.Structure], type[ctypes.Structure]]:
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    return Basic, Extended


def _kernel() -> Any:
    from ctypes import wintypes

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


class _Job:
    """A Windows Job Object; closing it kills every process still inside."""

    def __init__(self, kernel: Any, handle: int, memory_bytes: int | None) -> None:
        self._kernel = kernel
        self._handle: int | None = handle
        self.memory_bytes = memory_bytes

    def memory_exceeded(self) -> bool:
        """Whether a process in the job came within 32 MiB of the memory cap.

        A refused allocation leaves the peak just below the cap, less the size of
        the allocation that failed; sandbox allocations are far smaller than 32 MiB.
        """
        if self._handle is None or self.memory_bytes is None:
            return False
        _, extended = _structures()
        info = extended()
        if not self._kernel.QueryInformationJobObject(
            self._handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return False
        return int(info.peak_process) >= self.memory_bytes - _MEMORY_MARGIN

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel.CloseHandle(handle)


def contain(process: asyncio.subprocess.Process, *, memory_bytes: int | None) -> Closeable:
    """Put a just-spawned, still-idle process in a kill-on-close job with a memory cap.

    Windows: ``CreateJobObject`` plus ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` with
    ``KILL_ON_JOB_CLOSE`` (and ``PROCESS_MEMORY`` when ``memory_bytes`` is set),
    then ``AssignProcessToJobObject``. POSIX: a no-op handle (the worker calls
    ``limit_self``). Raises ``OSError`` when the job cannot be applied.
    """
    if sys.platform != "win32":
        return _Nothing()
    if memory_bytes is not None and (type(memory_bytes) is not int or memory_bytes < 2**20):
        raise ValueError("Invalid memory limit")
    kernel = _kernel()
    _, extended = _structures()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("Cannot create process job")
    info = extended()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.process_memory = memory_bytes
    info.basic.flags = flags
    handle = kernel.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process.pid,
    )
    try:
        if (
            not handle
            or not kernel.SetInformationJobObject(
                job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            )
            or not kernel.AssignProcessToJobObject(job, handle)
        ):
            kernel.CloseHandle(job)
            raise OSError("Cannot confine process to job")
    finally:
        if handle:
            kernel.CloseHandle(handle)
    return _Job(kernel, cast(int, job), memory_bytes)


def limit_self(memory_bytes: int) -> None:
    """POSIX: cap this process's address space with ``RLIMIT_AS``. Windows: no-op."""
    if sys.platform == "win32":
        return
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
    if soft == resource.RLIM_INFINITY or soft > limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
