"""Process-tree cleanup for spawned stdio servers (Windows and POSIX).

A server started through ``npx``, ``uvx`` or a shell wrapper has children of its own, so
stopping it means stopping the tree. POSIX: the server leads a new session, its group gets
SIGTERM, then SIGKILL. Windows: the server runs in its own process group inside a
kill-on-close job object, ``taskkill /T /F`` walks the tree, and closing the job ends
anything left.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from typing import Any, cast

__all__ = ["Job", "kill_tree", "spawn_options", "terminate_tree"]

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def spawn_options() -> dict[str, Any]:
    """Keyword arguments for ``create_subprocess_exec`` that isolate the server's tree."""
    if sys.platform == "win32":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


class Job:
    """A Windows kill-on-close job holding one process tree; a no-op elsewhere."""

    def __init__(self, pid: int) -> None:
        self.handle: int | None = None
        if sys.platform != "win32":
            return
        try:
            self.handle = _windows_job(pid)
        except OSError:
            self.handle = None  # taskkill still walks the tree on close

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None and sys.platform == "win32":
            kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)


def _windows_job(pid: int) -> int:
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

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed")
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process = kernel.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
    try:
        if (
            not process
            or not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            or not kernel.AssignProcessToJobObject(job, process)
        ):
            kernel.CloseHandle(job)
            raise OSError("job assignment failed")
    finally:
        if process:
            kernel.CloseHandle(process)
    return cast(int, job)


def terminate_tree(pid: int) -> None:
    """Ask the tree to stop (SIGTERM to the group; on Windows this is already forceful)."""
    if sys.platform == "win32":
        kill_tree(pid)
        return
    try:
        cast(Any, os).killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def kill_tree(pid: int) -> None:
    """Stop the tree now; a tree that is already gone is not an error."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=CREATE_NO_WINDOW, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        cast(Any, os).killpg(pid, cast(Any, signal).SIGKILL)
    except OSError:
        try:
            os.kill(pid, cast(Any, signal).SIGKILL)
        except OSError:
            pass
