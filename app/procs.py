"""Child-process hygiene on Windows.

pip's console-script shims (streamlink.exe, yt-dlp.exe) and the venv's python.exe are launchers
that spawn the real interpreter as a child. Terminating the launcher orphans that child, which
keeps downloading. So: always kill whole trees, and tie every descendant to our lifetime.
"""

import ctypes
import logging
import subprocess
import sys

log = logging.getLogger(__name__)
_job_handle = None   # keep alive for the process lifetime; closing it kills all children


def kill_tree(proc: subprocess.Popen, timeout: float = 15) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def contain_children() -> bool:
    """Put this process in a kill-on-close job: if the app exits or crashes, Windows kills every
    recorder/chat child with it. Children inherit job membership automatically."""
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return _job_handle is not None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = job and k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                             ctypes.byref(info), ctypes.sizeof(info)) \
        and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
    if not ok:
        log.warning("could not create kill-on-close job object (error %s)", ctypes.get_last_error())
        return False
    _job_handle = job
    return True
