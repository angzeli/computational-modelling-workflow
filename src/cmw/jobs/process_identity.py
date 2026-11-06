"""Clock-independent kernel evidence for a local Jobs process identity."""
import ctypes
import errno
from functools import cache
import os
from pathlib import Path
import socket
import sys
from uuid import UUID


class _Timeval(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_long), ('microseconds', ctypes.c_int32)]


class _KinfoPrefix(ctypes.Structure):
    # Darwin SDK sys/proc.h and sys/sysctl.h: kinfo_proc begins with
    # extern_proc, whose first union contains p_starttime (struct timeval).
    _fields_ = [('start', _Timeval), ('vmspace', ctypes.c_void_p),
                ('sigacts', ctypes.c_void_p), ('flags', ctypes.c_int),
                ('state', ctypes.c_byte), ('pid', ctypes.c_int)]


@cache
def _darwin_sysctls():
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    args = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, *args]
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, *args]
    return libc


def _raise_errno():
    number = ctypes.get_errno()
    raise OSError(number, os.strerror(number))


def boot_session_id():
    """Read the kernel boot UUID, independent of the wall-clock boot time."""
    if sys.platform == 'darwin':
        value = ctypes.create_string_buffer(64)
        size = ctypes.c_size_t(len(value))
        if _darwin_sysctls().sysctlbyname(b'kern.bootsessionuuid', value, ctypes.byref(size), None, 0):
            _raise_errno()
        raw = value.value
    elif sys.platform.startswith('linux'):
        raw = Path('/proc/sys/kernel/random/boot_id').read_bytes().strip()
    else:
        raise OSError(errno.ENOTSUP, 'Kernel boot UUID unavailable on this platform')
    try:
        return str(UUID(raw.decode('ascii')))
    except ValueError as exc:
        raise OSError(errno.EIO, 'Unreadable kernel boot UUID') from exc


class ConfirmedProcessAbsence(ProcessLookupError):
    """Identity lookup and an independent kernel PID query both found no process."""


def _confirm_absence(pid):
    # An empty sysctl result or a missing /proc entry can also mean that the
    # observer cannot see the process. Only a kernel ESRCH establishes absence.
    try:
        os.getpgid(pid)
    except ProcessLookupError as exc:
        raise ConfirmedProcessAbsence(errno.ESRCH, 'PID is absent') from exc
    raise PermissionError(errno.EACCES, 'Process exists but kernel identity is unavailable')


def _darwin_process(pid):
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    data = ctypes.create_string_buffer(4096)
    size = ctypes.c_size_t(len(data))
    if _darwin_sysctls().sysctl(mib, len(mib), data, ctypes.byref(size), None, 0):
        if ctypes.get_errno() == errno.ESRCH:
            _confirm_absence(pid)
        _raise_errno()
    if size.value == 0:
        _confirm_absence(pid)
    if size.value < ctypes.sizeof(_KinfoPrefix):
        raise OSError(errno.EIO, 'Incomplete kernel process record')
    row = _KinfoPrefix.from_buffer(data)
    if (row.pid != pid or row.start.seconds <= 0 or not 0 <= row.start.microseconds < 1_000_000
            or row.state not in (1, 2, 3, 4, 5)):
        raise OSError(errno.EIO, 'Inconsistent kernel process record')
    return row.start.seconds * 1_000_000 + row.start.microseconds, row.state == 5  # SZOMB


def _linux_process(pid):
    try:
        record = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        _confirm_absence(pid)
    # comm may contain spaces, parentheses, newlines or non-UTF-8 bytes.
    prefix, separator, tail = record.rpartition(b') ')
    fields = tail.split()
    if not separator or not prefix.startswith(f'{pid} ('.encode()) or len(fields) < 20:
        raise OSError(errno.EIO, 'Incomplete kernel process record')
    if fields[0] not in (b'R', b'S', b'D', b'Z', b'T', b't', b'X', b'x', b'K', b'W', b'P', b'I') or not fields[19].isdigit():
        raise OSError(errno.EIO, 'Invalid kernel process state or start token')
    return int(fields[19]), fields[0] in (b'Z', b'X', b'x')


def process_evidence(pid):
    """Return the raw start token and zombie/dead status from the same kernel record."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        raise ValueError('Process identity requires a positive PID')
    if sys.platform == 'darwin':
        return _darwin_process(pid)
    if sys.platform.startswith('linux'):
        return _linux_process(pid)
    raise OSError(errno.ENOTSUP, 'Kernel process identity unavailable on this platform')


def capture(pid=None):
    pid = os.getpid() if pid is None else pid
    return {'version': 2, 'host': socket.gethostname(), 'boot_session': boot_session_id(),
            'pid': pid, 'start_token': process_evidence(pid)[0]}
