"""Confine validation subprocesses to a kill-on-close Windows Job Object."""
import contextlib
import ctypes
from ctypes import wintypes
import os


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


@contextlib.contextmanager
def child_job():
    if os.name != "nt":
        # The portable integrity and fixture tests are also runnable on Linux.
        yield lambda process: None
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateJobObjectW
    create.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    configure = kernel.SetInformationJobObject
    configure.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    configure.restype = wintypes.BOOL
    assign = kernel.AssignProcessToJobObject
    assign.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    handle = create(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not configure(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        def attach(process):
            if not assign(handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())

        yield attach
    finally:
        close(handle)
