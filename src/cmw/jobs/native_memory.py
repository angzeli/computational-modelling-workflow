"""Optional, unprivileged macOS physical-footprint observation.

The public libproc RUSAGE_INFO_V0 layout is stable since macOS 10.9. This is
process accounting, not unique resident RAM or an Activity Monitor equivalence.
"""
from __future__ import annotations

import ctypes
import os
import sys


class RUsageInfoV0(ctypes.Structure):
    _fields_ = [('ri_uuid', ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            'ri_user_time', 'ri_system_time', 'ri_pkg_idle_wkups',
            'ri_interrupt_wkups', 'ri_pageins', 'ri_wired_size',
            'ri_resident_size', 'ri_phys_footprint', 'ri_proc_start_abstime',
            'ri_proc_exit_abstime')]


def footprint_reader(*, platform=sys.platform):
    """Return one bounded syscall per PID, or None when unsupported.

    The caller must bracket the read with its existing process birth checks.
    Permission and exit errors remain errors; an unreadable footprint is not zero.
    """
    if platform != 'darwin':
        return None
    try:
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        rusage = library.proc_pid_rusage
        rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        rusage.restype = ctypes.c_int
    except (OSError, AttributeError):
        return None

    def read(pid):
        record = RUsageInfoV0()
        if rusage(pid, 0, ctypes.byref(record)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return int(record.ri_phys_footprint)

    return read
