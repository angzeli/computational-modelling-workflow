"""Bounded observations supplement runtime trust; they never replace enrollment."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys

from cmw.core.provenance import stable_hash


class RuntimeRefusal(ValueError):
    def __init__(self, category, *, phase, before=None, after=None, path=None,
                 expected_digest=None, actual_digest=None):
        self.evidence = dict(category=category, role="runtime-package", phase=phase, path=path, before=before,
                            after=after, expected_digest=expected_digest, actual_digest=actual_digest)
        super().__init__('CMW runtime verification refused: ' + category)


def _extended_security(path):
    """Read macOS ACL/xattrs natively; unavailable evidence is a refusal."""
    if sys.platform != 'darwin' and hasattr(os, 'listxattr'):
        names = os.listxattr(path, follow_symlinks=False)
        if len(names) > 64:
            raise RuntimeRefusal('security_metadata_bound', phase='xattr-list', path=str(path))
        values = {}
        for name in names:
            data = os.getxattr(path, name, follow_symlinks=False)
            if len(data) > 1024 * 1024:
                raise RuntimeRefusal('security_metadata_bound', phase='xattr-read', path=str(path))
            values[name] = hashlib.sha256(data).hexdigest()
        return values
    if sys.platform != 'darwin':
        raise RuntimeRefusal('security_metadata_unavailable', phase='inventory', path=str(path))
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    listing, getting = libc.listxattr, libc.getxattr
    listing.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    listing.restype = ctypes.c_ssize_t
    getting.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_uint32, ctypes.c_int]
    getting.restype = ctypes.c_ssize_t
    raw = os.fsencode(path)
    size = listing(raw, None, 0, 1)
    if size < 0 or size > 65536:
        raise RuntimeRefusal('security_metadata_unavailable', phase='xattr-list', path=str(path))
    buffer = ctypes.create_string_buffer(size)
    if listing(raw, buffer, size, 1) != size:
        raise RuntimeRefusal('in_flight_change', phase='xattr-list', path=str(path))
    names = buffer.raw.rstrip(b'\0').split(b'\0') if size else []
    if len(names) > 64:
        raise RuntimeRefusal('security_metadata_bound', phase='xattr-list', path=str(path))
    attrs = {}
    for name in names:
        length = getting(raw, name, None, 0, 0, 1)
        if length < 0 or length > 1024 * 1024:
            raise RuntimeRefusal('security_metadata_unavailable', phase='xattr-read', path=str(path))
        value = ctypes.create_string_buffer(length)
        if getting(raw, name, value, length, 0, 1) != length:
            raise RuntimeRefusal('in_flight_change', phase='xattr-read', path=str(path))
        attrs[os.fsdecode(name)] = hashlib.sha256(value.raw).hexdigest()
    get_acl, to_text, free = libc.acl_get_file, libc.acl_to_text, libc.acl_free
    get_acl.argtypes, get_acl.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_void_p
    to_text.argtypes, to_text.restype = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)], ctypes.c_void_p
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(raw, 0x100)
    if not acl:
        # NULL/ENOENT on an existing object is not documented proof of ACL absence.
        # Preserve strict epoch observation, but prohibit metadata recovery.
        attrs['__cmw_acl__'] = 'UNAVAILABLE:ACL'
        return attrs
    text = None
    try:
        length = ctypes.c_ssize_t()
        text = to_text(acl, ctypes.byref(length))
        if not text or not 0 <= length.value <= 1024 * 1024:
            raise RuntimeRefusal('security_metadata_unavailable', phase='acl-read', path=str(path))
        attrs['__cmw_acl__'] = hashlib.sha256(ctypes.string_at(text, length.value)).hexdigest()
    finally:
        if text:
            free(text)
        free(acl)
    return attrs


def _security(path, info):
    extended = _extended_security(path)
    complete = not str(extended.get('__cmw_acl__', '')).startswith('UNAVAILABLE:')
    return (info.st_mode, info.st_uid, info.st_gid, getattr(info, 'st_flags', 0),
            info.st_nlink, stable_hash(extended), complete)


def file_epoch(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeRefusal('nonregular_member', phase='inventory', path=str(path))
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            *_security(path, info))


def inventory(root):
    root = Path(root)
    info = root.lstat()
    if root != root.resolve(strict=True) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeRefusal('root_replacement', phase='inventory', path=str(root))
    # Directory modification times change for excluded bytecode; object and security do not.
    security = _security(root, info)
    root_epoch = (info.st_dev, info.st_ino, *security[:4], *security[-2:])
    entries = sorted(path for path in root.rglob('*')
                     if '__pycache__' not in path.parts)
    if len(entries) > 8192:
        raise RuntimeRefusal('membership_bound', phase='inventory', path=str(root))
    directories = {}
    for path in entries:
        if path.is_dir():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeRefusal('path_replacement', phase='inventory', path=str(path))
            security = _security(path, info)
            directories[str(path.relative_to(root))] = (info.st_dev, info.st_ino, *security[:4], *security[-2:])
    paths = sorted(path for path in entries
                   if '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.pyo'}
                   and path.name != '.DS_Store' and (path.is_file() or path.is_symlink()))
    if not paths or len(paths) > 4096:
        raise RuntimeRefusal('membership_bound', phase='inventory', path=str(root))
    epoch, total = [], 0
    for path in paths:
        if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise RuntimeRefusal('path_replacement', phase='inventory', path=str(path))
        observed = file_epoch(path)
        total += observed[2]
        if observed[2] > 16 * 1024**2 or total > 128 * 1024**2:
            raise RuntimeRefusal('member_size_bound', phase='inventory', path=str(path))
        epoch.append((str(path.relative_to(root)), observed))
    return root_epoch, tuple(epoch), directories


def observation(root, root_epoch, epoch, directories, digest):
    executable = Path(sys.executable).resolve(strict=True)
    return dict(schema=1, root=str(root), root_epoch=list(root_epoch),
                executable=str(executable), executable_epoch=list(file_epoch(executable)),
                members={name: list(value) for name, value in epoch},
                directories={name: list(value) for name, value in directories.items()}, package_sha256=digest)


def _valid_epoch(value, length):
    return (type(value) is list and len(value) == length
            and all(type(item) is int and item >= 0 for item in value[:-2])
            and type(value[-2]) is str and len(value[-2]) == 64
            and all(c in '0123456789abcdef' for c in value[-2])
            and type(value[-1]) is bool)


def validate_observation(value):
    fields = {'schema', 'root', 'root_epoch', 'executable', 'executable_epoch',
              'members', 'directories', 'package_sha256'}
    if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1 or type(value['members']) is not dict
            or not 1 <= len(value['members']) <= 4096
            or type(value['directories']) is not dict or len(value['directories']) > 4096):
        raise ValueError('Malformed original runtime observation')
    for name, epoch in list(value['members'].items()) + list(value['directories'].items()):
        if type(name) is not str:
            raise ValueError('Malformed original runtime member path')
        path = Path(name)
        if (not name or path.is_absolute() or '..' in path.parts
                or str(path) != name or not _valid_epoch(epoch, 12 if name in value['members'] else 8)):
            raise ValueError('Malformed original runtime member or directory')
    if (not _valid_epoch(value['root_epoch'], 8) or not _valid_epoch(value['executable_epoch'], 12)
            or any(type(value[key]) is not str for key in ('root', 'executable', 'package_sha256'))
            or not Path(value['root']).is_absolute() or not Path(value['executable']).is_absolute()
            or len(value['package_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['package_sha256'])):
        raise ValueError('Malformed original runtime root/interpreter')
    return value


def _source_imports_coherent(observed):
    root = Path(observed['root'])
    for name, module in tuple(sys.modules.items()):
        if name != 'cmw' and not name.startswith('cmw.'):
            continue
        filename = getattr(module, '__file__', None)
        if filename is None:
            continue
        path = Path(filename).resolve()
        if not path.is_relative_to(root) or str(path.relative_to(root)) not in observed['members']:
            return False
        cached = getattr(module, '__cached__', None)
        if path.suffix != '.py' or cached and Path(cached).exists():
            return False
    return True


def _differences(before, after):
    if before['root'] != after['root'] or before['root_epoch'] != after['root_epoch']:
        return 'root_replacement'
    if before['directories'] != after['directories']:
        return 'directory_identity_or_security_changed'
    if (before['executable'] != after['executable']
            or before['executable_epoch'] != after['executable_epoch']):
        return 'interpreter_changed'
    if set(before['members']) != set(after['members']):
        return 'membership_changed'
    for name, old in before['members'].items():
        new = after['members'][name]
        if old[:2] != new[:2]:
            return 'member_replaced'
        if old[5:] != new[5:]:
            return 'security_metadata_changed'
        if old[2:4] != new[2:4]:
            return 'content_epoch_changed'
    if before['package_sha256'] != after['package_sha256']:
        return 'content_changed'
    return None


class RuntimeVerifier:
    """One original baseline, one metadata recovery; imported code is separate evidence."""
    def __init__(self, expected, original=None, *, process_started_ns=None, isolated=None):
        self.expected = expected
        self.original = validate_observation(original) if original is not None else None
        self.current = self.original
        self.recovered = False
        self.last_evidence = None
        if process_started_ns is None:
            import psutil
            process_started_ns = int(psutil.Process().create_time() * 1e9)
        self.process_started_ns = process_started_ns
        self.isolated = bool(sys.flags.isolated and sys.dont_write_bytecode) if isolated is None else isolated

    def verify(self, provider):
        try:
            record, observed = provider()
        except RuntimeRefusal as exc:
            exc.evidence["trusted_baseline"] = self.original
            exc.evidence["expected_runtime"] = self.expected
            self.last_evidence = exc.evidence
            raise
        baseline = self.current
        if record['fingerprint'] != self.expected['fingerprint']:
            category = _differences(baseline, observed) if baseline is not None else None
            raise RuntimeRefusal(category or 'runtime_fingerprint_changed', phase='compare',
                before=baseline, after=observed, expected_digest=self.expected['fingerprint'],
                actual_digest=record['fingerprint'])
        if baseline is None:
            # Legacy enrollment permits ordinary checks, not metadata recovery.
            self.current = observed
            return record
        if baseline == observed:
            return record
        category = _differences(baseline, observed)
        if category is None and (not baseline['root_epoch'][-1] or
                not baseline['executable_epoch'][-1] or
                not all(epoch[-1] for epoch in baseline['members'].values()) or
                not all(epoch[-1] for epoch in baseline['directories'].values())):
            category = 'security_baseline_incomplete'
        if category is None and self.original is None:
            category = 'original_baseline_unavailable'
        if category is None and self.recovered:
            category = 'metadata_recovery_exhausted'
        self.last_evidence = dict(before=baseline, after=observed,
            expected_digest=self.expected['package_sha256'], actual_digest=record['package_sha256'])
        if category:
            raise RuntimeRefusal(category, phase='compare', **self.last_evidence)
        # Provider has read the complete changed epoch and rechecked all membership.
        # A long-lived interpreter cannot be made coherent by hashing its disk files.
        changed = [new[4] for name, new in observed['members'].items()
                   if new != baseline['members'][name]]
        if (not self.isolated or not changed or max(changed) >= self.process_started_ns
                or not _source_imports_coherent(observed)):
            raise RuntimeRefusal('fresh_isolated_observer_required', phase='loaded-code',
                                 **self.last_evidence)
        self.current = observed
        self.recovered = True
        self.last_evidence['category'] = 'ctime_only_current_disk_verified'
        return record
