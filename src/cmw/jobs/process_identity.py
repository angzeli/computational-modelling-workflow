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


def _linux_fields(pid):
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
    return fields


def _linux_process(pid):
    fields = _linux_fields(pid)
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


@cache
def _darwin_libproc():
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    fn = libproc.proc_listpids
    fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    return libproc


def _children(pid):
    if sys.platform == 'darwin':
        # libproc's PROC_PPID_ONLY returns bytes of child PIDs. A full buffer
        # may have been truncated, so grow it or refuse the incomplete census.
        capacity = 32
        while capacity <= 2**18:
            buf = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            size = _darwin_libproc().proc_listpids(6, pid, buf, ctypes.sizeof(buf))
            if size < 0 or (size == 0 and ctypes.get_errno()):
                _raise_errno()
            if size < ctypes.sizeof(buf) and size % ctypes.sizeof(ctypes.c_int) == 0:
                return buf[:size // ctypes.sizeof(ctypes.c_int)]
            capacity *= 2
        raise OSError(errno.EOVERFLOW, 'Incomplete kernel child PID list')
    if sys.platform.startswith('linux'):
        # Linux lists children per *thread*. Include non-leader parents.
        tasks = Path(f'/proc/{pid}/task')
        return {int(child) for thread in tasks.iterdir()
                for child in (thread / 'children').read_bytes().split()}
    raise OSError(errno.ENOTSUP, 'Kernel child PID list unavailable')


def _parent(pid):
    if sys.platform == 'darwin':
        # PROC_PIDT_SHORTBSDINFO begins with two uint32_t fields, PID and PPID.
        buf = (ctypes.c_uint32 * 64)()
        ctypes.set_errno(0)
        size = _darwin_libproc().proc_pidinfo(pid, 13, 0, buf, ctypes.sizeof(buf))
        if size <= 0:
            if ctypes.get_errno() == errno.ESRCH:
                _confirm_absence(pid)
            if ctypes.get_errno():
                _raise_errno()
        if size < 8 or buf[0] != pid:
            raise OSError(errno.EIO, 'Incomplete kernel parent identity')
        return buf[1]
    if sys.platform.startswith('linux'):
        fields = _linux_fields(pid)
        if not fields[1].isdigit():
            raise OSError(errno.EIO, 'Invalid kernel parent PID')
        return int(fields[1])
    raise OSError(errno.ENOTSUP, 'Kernel parent PID unavailable')


def _check_edges(nodes, edges):
    # A PID can be recycled or reparented during either walk. Compare raw
    # identities for equality, never order wall-clock birth times.
    for pid, ppid in edges:
        if _parent(pid) != ppid:
            raise OSError(errno.EAGAIN, 'Process parent changed during ancestry observation')
    for pid, evidence in nodes.items():
        if process_evidence(pid) != evidence:
            raise OSError(errno.EAGAIN, 'Process identity changed during ancestry observation')


def ancestor_pids(pid, stop_pid):
    """Return nearest parents up to stop_pid, bound to unchanged kernel identities."""
    nodes = {pid: process_evidence(pid)}
    edges = []
    while pid != stop_pid:
        parent = _parent(pid)
        if parent == 0:
            break
        if parent in nodes:
            raise OSError(errno.EAGAIN, 'Inconsistent kernel parent chain')
        nodes[parent] = process_evidence(parent)
        edges.append((pid, parent))
        pid = parent
    _check_edges(nodes, edges)
    return tuple(parent for _, parent in edges)


def descendant_pids(root_pid):
    """Census live descendants, refusing unverifiable parentage or PID reuse."""
    nodes = {root_pid: process_evidence(root_pid)}
    edges = []
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        for child in _children(pid):
            if child in nodes:
                raise OSError(errno.EAGAIN, 'Inconsistent kernel child list')
            nodes[child] = process_evidence(child)
            edges.append((child, pid))
            if not nodes[child][1]:
                pending.append(child)
    _check_edges(nodes, edges)
    return tuple(pid for pid, _ in edges if not nodes[pid][1])
