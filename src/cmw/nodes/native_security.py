"""Descriptor-bound Darwin ACL evidence; errors never mean ACL absence."""
from __future__ import annotations

import ctypes as C
import errno
import hashlib
import os
import platform
import sys


class SecurityReadError(OSError):
    def __init__(self, api, code, state='error'):
        super().__init__(code, f'{api}: {state}')
        self.api, self.state = api, state


class _Timespec(C.Structure):
    _fields_ = [('seconds', C.c_int64), ('nanoseconds', C.c_int64)]


class _Stat64(C.Structure):
    # Darwin SDK sys/stat.h __DARWIN_STRUCT_STAT64, arm64/x86_64 ABI.
    _fields_ = [('dev', C.c_int32), ('mode', C.c_uint16), ('nlink', C.c_uint16),
                ('ino', C.c_uint64), ('uid', C.c_uint32), ('gid', C.c_uint32),
                ('rdev', C.c_int32), ('atime', _Timespec), ('mtime', _Timespec),
                ('ctime', _Timespec), ('birthtime', _Timespec), ('size', C.c_int64),
                ('blocks', C.c_int64), ('blksize', C.c_int32), ('flags', C.c_uint32),
                ('gen', C.c_uint32), ('spare', C.c_int32), ('qspare', C.c_int64 * 2)]


def object_epoch(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, 'st_flags', 0))


def _bind_api():
    if sys.platform != 'darwin' or platform.machine() not in {'arm64', 'x86_64'}:
        return None
    libc = C.CDLL(None, use_errno=True)
    signatures = {
        'flistxattr': ([C.c_int, C.c_void_p, C.c_size_t, C.c_int], C.c_ssize_t),
        'fgetxattr': ([C.c_int, C.c_char_p, C.c_void_p, C.c_size_t, C.c_uint32, C.c_int], C.c_ssize_t),
        'filesec_init': ([], C.c_void_p),
        'filesec_free': ([C.c_void_p], None),
        'fstatx_np': ([C.c_int, C.POINTER(_Stat64), C.c_void_p], C.c_int),
        'filesec_query_property': ([C.c_void_p, C.c_int, C.POINTER(C.c_int)], C.c_int),
        'filesec_get_property': ([C.c_void_p, C.c_int, C.c_void_p], C.c_int),
        'acl_to_text': ([C.c_void_p, C.POINTER(C.c_ssize_t)], C.c_void_p),
        'acl_free': ([C.c_void_p], C.c_int),
    }
    try:
        for name, (args, result) in signatures.items():
            fn = getattr(libc, name)
            fn.argtypes, fn.restype = args, result
    except AttributeError:
        return None
    return libc


# The import lock serializes binding. Descriptors and their evidence stay local to each read.
_LIBC = _bind_api()


def _api():
    if _LIBC is None:
        raise SecurityReadError('Darwin security API', errno.ENOTSUP, 'unsupported')
    return _LIBC


def _failed(api):
    code = C.get_errno()
    state = ('denied' if code in {errno.EACCES, errno.EPERM} else
             'unsupported' if code in {errno.ENOTSUP, errno.ENOSYS} else 'error')
    raise SecurityReadError(api, code, state)


def acl_evidence(fd, *, libc=None, observe=None):
    """Absence requires successful fstatx AND successful ACL-property query == 0.

    Libc statx1 clears FS_VALID_ACL for KAUTH_FILESEC_NOACL/no ACL payload;
    filesec_query_property exposes that bit. acl_get_file's NULL/ENOENT alone
    does not distinguish this case from failure. A fresh filesec owns its memory.
    """
    libc = _api() if libc is None else libc
    before = os.fstat(fd)
    if observe is not None:
        observe(object_epoch(before), 'acl-before')
    C.set_errno(0)
    sec = libc.filesec_init()
    if not sec:
        _failed('filesec_init')
    acl, text = C.c_void_p(), None
    try:
        native = _Stat64()
        C.set_errno(0)
        if libc.fstatx_np(fd, C.byref(native), sec) != 0:
            _failed('fstatx_np')
        native_epoch = (native.dev, native.ino, native.mode, native.uid, native.gid,
                        native.nlink, native.size,
                        native.mtime.seconds * 10**9 + native.mtime.nanoseconds,
                        native.ctime.seconds * 10**9 + native.ctime.nanoseconds, native.flags)
        if observe is not None:
            observe(native_epoch, 'acl-native')
        elif native_epoch != object_epoch(before):
            raise SecurityReadError('fstatx_np', 0, 'changed-object')
        present = C.c_int(-1)
        C.set_errno(0)
        if libc.filesec_query_property(sec, 5, C.byref(present)) != 0:
            _failed('filesec_query_property')
        if present.value == 0:
            result = {'state': 'absent', 'api': 'fstatx_np/filesec_query_property', 'errno': 0}
        elif present.value == 32:  # FS_VALID_ACL, not a normalized boolean.
            C.set_errno(0)
            if libc.filesec_get_property(sec, 5, C.byref(acl)) != 0 or not acl.value or acl.value == 1:
                _failed('filesec_get_property')
            length = C.c_ssize_t()
            C.set_errno(0)
            text = libc.acl_to_text(acl, C.byref(length))
            if not text:
                _failed('acl_to_text')
            if not 0 <= length.value <= 1024 * 1024:
                raise SecurityReadError('acl_to_text', errno.EOVERFLOW, 'bound')
            # Native serialization retains ACE order, qualifiers and inheritance.
            result = {'state': 'present', 'api': 'fstatx_np/filesec_query_property',
                      'errno': 0, 'sha256': hashlib.sha256(C.string_at(text, length.value)).hexdigest()}
        else:
            raise SecurityReadError('filesec_query_property', errno.EINVAL, 'unsupported-result')
        after = object_epoch(os.fstat(fd))
        if observe is not None:
            observe(after, 'acl-after')
        elif after != object_epoch(before):
            raise SecurityReadError('acl-read', 0, 'changed-object')
        return result
    finally:
        if text:
            libc.acl_free(text)
        if acl.value and acl.value != 1:
            libc.acl_free(acl)
        libc.filesec_free(sec)
