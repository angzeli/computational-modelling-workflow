"""Bounded observations supplement runtime trust; they never replace enrollment."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import sys
import time

from cmw.core.provenance import stable_hash


class RuntimeRefusal(ValueError):
    def __init__(self, category, *, phase, before=None, after=None, path=None,
                 expected_digest=None, actual_digest=None, api=None, errno=None, security_state=None, object_type=None):
        self.evidence = dict(category=category, role="runtime-package", phase=phase, path=path, before=before,
                            after=after, expected_digest=expected_digest, actual_digest=actual_digest, api=api, errno=errno,
                            security_state=security_state, object_type=object_type)
        super().__init__('CMW runtime verification refused: ' + category)


def _extended_security(path, *, observe=None):
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
    from .native_security import acl_evidence, object_epoch, SecurityReadError
    libc = ctypes.CDLL(None, use_errno=True)
    listing, getting = libc.flistxattr, libc.fgetxattr
    listing.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    listing.restype = ctypes.c_ssize_t
    getting.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_uint32, ctypes.c_int]
    getting.restype = ctypes.c_ssize_t
    before = path.lstat()
    if observe is not None:
        observe(object_epoch(before), 'security-before')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeRefusal('security_metadata_unavailable', phase='security-open', path=str(path),
                             api='open', errno=exc.errno,
                             security_state='denied' if isinstance(exc, PermissionError) else 'error',
                             object_type='directory' if stat.S_ISDIR(before.st_mode) else 'file') from exc
    try:
        opened = object_epoch(os.fstat(descriptor))
        if observe is not None:
            observe(opened, 'security-open')
        elif opened != object_epoch(before):
            raise RuntimeRefusal('in_flight_change', phase='security-open', path=str(path))
        size = listing(descriptor, None, 0, 0)
        if size < 0 or size > 65536:
            raise RuntimeRefusal('security_metadata_unavailable', phase='xattr-list', path=str(path),
                                 api='flistxattr', errno=ctypes.get_errno())
        buffer = ctypes.create_string_buffer(size)
        if listing(descriptor, buffer, size, 0) != size:
            raise RuntimeRefusal('in_flight_change', phase='xattr-list', path=str(path))
        names = buffer.raw.rstrip(b'\0').split(b'\0') if size else []
        if len(names) > 64:
            raise RuntimeRefusal('security_metadata_bound', phase='xattr-list', path=str(path))
        attrs = {}
        for name in names:
            length = getting(descriptor, name, None, 0, 0, 0)
            if length < 0 or length > 1024 * 1024:
                raise RuntimeRefusal('security_metadata_unavailable', phase='xattr-read', path=str(path),
                                     api='fgetxattr', errno=ctypes.get_errno())
            value = ctypes.create_string_buffer(length)
            if getting(descriptor, name, value, length, 0, 0) != length:
                raise RuntimeRefusal('in_flight_change', phase='xattr-read', path=str(path))
            attrs[os.fsdecode(name)] = hashlib.sha256(value.raw).hexdigest()
        try:
            acl = acl_evidence(descriptor, observe=observe) if observe is not None else acl_evidence(descriptor)
        except SecurityReadError as exc:
            raise RuntimeRefusal('in_flight_change' if exc.state == 'changed-object' else
                                 'security_metadata_unavailable', phase='acl-read', path=str(path),
                                 api=exc.api, errno=exc.errno, security_state=exc.state,
                                 object_type='directory' if stat.S_ISDIR(before.st_mode) else 'file') from exc
        attrs['__cmw_acl__'] = 'ABSENT:FILESEC_ACL' if acl['state'] == 'absent' else acl['sha256']
        descriptor_after, path_after = os.fstat(descriptor), path.lstat()
        if observe is not None:
            observe(object_epoch(descriptor_after), 'security-descriptor-after')
            observe(object_epoch(path_after), 'security-path-after')
        elif (object_epoch(descriptor_after) != object_epoch(before)
                or object_epoch(path_after) != object_epoch(before)):
            raise RuntimeRefusal('in_flight_change', phase='security-after-read', path=str(path),
                before=object_epoch(before), after={'descriptor': object_epoch(descriptor_after),
                    'path': object_epoch(path_after), 'acl': acl},
                actual_digest=_security_digest(attrs), api='fstat/lstat', security_state='complete',
                object_type='directory' if stat.S_ISDIR(before.st_mode) else 'file')
        return attrs
    finally:
        os.close(descriptor)


def _security_digest(extended):
    # Preserve schema-1 hash bytes; completeness/schema carry proven ACL absence.
    if extended.get('__cmw_acl__') == 'ABSENT:FILESEC_ACL':
        extended = {**extended, '__cmw_acl__': 'UNAVAILABLE:ACL'}
    return stable_hash(extended)


def _security(path, info):
    extended = _extended_security(path)
    complete = not str(extended.get('__cmw_acl__', '')).startswith('UNAVAILABLE:')
    return (info.st_mode, info.st_uid, info.st_gid, getattr(info, 'st_flags', 0),
            info.st_nlink, _security_digest(extended), complete)


def file_epoch(path, *, collection=None):
    from .native_security import object_epoch
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeRefusal('nonregular_member', phase='inventory', path=str(path))
    if collection is None:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                *_security(path, info))
    observe = lambda value, phase: collection.object(path, value, phase)
    observe(object_epoch(info), 'stat-before')
    extended = _extended_security(path, observe=observe)
    after = path.lstat()
    observe(object_epoch(after), 'stat-after')
    epoch = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
             after.st_mode, after.st_uid, after.st_gid, getattr(after, 'st_flags', 0),
             after.st_nlink, _security_digest(extended),
             not str(extended.get('__cmw_acl__', '')).startswith('UNAVAILABLE:'))
    collection.security(path, epoch)
    return epoch


# Darwin SDK sys/stat.h: document-ID tracking. Never apply to other object kinds.
UF_TRACKED = 0x40
MAX_OBSERVATION_SECONDS = 10.0
MAX_READ_BYTES = 256 * 1024**2


def _member_difference(old, new):
    if old[:2] != new[:2]:
        return 'member_replaced'
    if old[5:8] != new[5:8] or old[9:] != new[9:]:
        return 'security_metadata_changed'
    if old[2:4] != new[2:4]:
        return 'content_epoch_changed'
    if old == new:
        return None
    if (sys.platform == 'darwin' and stat.S_ISREG(old[5]) and stat.S_ISREG(new[5])
            and old[-1] and new[-1] and old[8] ^ new[8] == UF_TRACKED):
        return None
    return 'unqualified_observation_change'


class RuntimeCollection:
    """One sequential verification: no package restarts, at most two reads/member."""
    def __init__(self, root, diagnostics=None):
        self.root = root
        self.deadline = time.monotonic() + MAX_OBSERVATION_SECONDS
        self.objects, self.epochs, self.hashes, self.read_epochs = {}, {}, {}, {}
        self.transitions, self.reads = {}, {}
        self.strict_members = set()
        self.bytes_read = 0
        self.generation = 'initial-inventory'
        self.evidence = {'policy': 3, 'status': 'PENDING', 'transitions': [],
                         'content_reads': self.reads, 'bytes_read': 0}
        if diagnostics is not None:
            diagnostics.append(self.evidence)

    def check(self):
        if time.monotonic() > self.deadline:
            self.refuse('observation_deadline', str(self.root))

    def refuse(self, category, path, before=None, after=None, *, phase=None):
        error = RuntimeRefusal(category, phase=phase or self.generation, path=str(path),
                               before=before, after=after)
        error.evidence.update(policy=3, generation=self.generation,
                              bytes_read=self.bytes_read, content_reads=dict(self.reads))
        if isinstance(before, (tuple, list)) and isinstance(after, (tuple, list)) and len(before) == len(after):
            error.evidence['changed_indices'] = [i for i, pair in enumerate(zip(before, after))
                                                if pair[0] != pair[1]]
        self.evidence.update(status='REFUSED', refusal=error.evidence)
        raise error

    def object(self, path, value, phase):
        self.check()
        name = str(path.relative_to(self.root))
        previous = self.objects.get(name)
        if previous is not None and previous != value:
            if name in self.strict_members:
                self.refuse('auxiliary_changed', name, previous, value, phase=phase)
            eligible = (sys.platform == 'darwin' and stat.S_ISREG(previous[2])
                and stat.S_ISREG(value[2]) and previous[:8] == value[:8]
                and previous[9] ^ value[9] == UF_TRACKED)
            if not eligible:
                self.refuse('in_flight_change', name, previous, value, phase=phase)
            if name in self.transitions:
                self.refuse('member_observation_budget', name, previous, value, phase=phase)
            event = {'path': name, 'phase': phase, 'generation': self.generation,
                     'before': list(previous), 'after': list(value),
                     'raw_differences': {key: {'before': previous[index], 'after': value[index]}
                         for key, index in [('ctime_ns', 8), ('flags', 9)] if previous[index] != value[index]},
                     'material_differences': {}}
            self.transitions[name] = event
            self.evidence['transitions'].append(event)
        self.objects[name] = value

    def security(self, path, epoch):
        name = str(path.relative_to(self.root))
        if not epoch[-1]:
            self.refuse('security_metadata_incomplete', name, after=epoch)
        before = self.epochs.get(name)
        if before is not None:
            if name in self.strict_members and before != epoch:
                self.refuse('auxiliary_changed', name, before, epoch)
            category = _member_difference(before, epoch)
            if category:
                self.refuse(category, name, before, epoch)
        self.epochs[name] = epoch

    def read(self, path):
        from .native_security import object_epoch
        name = str(path.relative_to(self.root))
        before = file_epoch(path, collection=self)
        count = self.reads.get(name, 0)
        if count >= 2:
            self.refuse('member_read_budget', name)
        if before[2] > 16 * 1024**2 or self.bytes_read + before[2] > MAX_READ_BYTES:
            self.refuse('member_size_bound', name)
        self.reads[name] = count + 1
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as handle:
            self.object(path, object_epoch(os.fstat(handle.fileno())), 'content-open')
            data = handle.read(16 * 1024**2 + 1)
            self.object(path, object_epoch(os.fstat(handle.fileno())), 'content-after')
        self.bytes_read += len(data)
        self.evidence['bytes_read'] = self.bytes_read
        if len(data) != before[2] or self.bytes_read > MAX_READ_BYTES:
            self.refuse('incomplete_content_read', name)
        after = file_epoch(path, collection=self)
        digest = hashlib.sha256(data).hexdigest()
        if name in self.hashes and self.hashes[name] != digest:
            self.refuse('content_changed', name, self.hashes[name], digest)
        self.hashes[name] = digest
        self.read_epochs[name] = after
        if before != after:
            # A transition enclosing the read needs content evidence afterwards.
            return self.read(path)
        self.check()
        return data, after

    def final_member(self, path, epoch):
        name = str(path.relative_to(self.root))
        if name not in self.read_epochs:
            self.refuse('membership_changed', name)
        if epoch != self.read_epochs[name]:
            # Rehash this member after a qualified late delta, never restart peers.
            _, epoch = self.read(path)
        return epoch


def _directory_epoch(path, info, security, epochs):
    """Request-local directory epochs supplement the persisted security record."""
    from .native_security import object_epoch
    after = path.lstat()
    if object_epoch(info) != object_epoch(after):
        raise RuntimeRefusal('directory_changed', phase='directory-boundary', path=str(path),
                             before=object_epoch(info), after=object_epoch(after))
    if not security[-1]:
        raise RuntimeRefusal('security_metadata_incomplete', phase='directory-boundary', path=str(path))
    epochs[str(path)] = object_epoch(after)


def _auxiliary_member(path):
    return ('__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}
            or path.name == '.DS_Store')


def _request_entries(root, collection):
    """Bounded traversal of every entry, without following cache symlinks."""
    from .native_security import object_epoch
    entries, walked = [], {}
    def failed(error):
        raise error
    for parent, directories, files, descriptor in os.fwalk(root, follow_symlinks=False, onerror=failed):
        parent = Path(parent)
        if collection is not None:
            collection.check()
        if object_epoch(parent.lstat()) != object_epoch(os.fstat(descriptor)):
            raise RuntimeRefusal('directory_changed', phase='inventory', path=str(parent))
        walked[str(parent)] = object_epoch(os.fstat(descriptor))
        for name in directories + files:
            path = parent / name
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise RuntimeRefusal('nonregular_member', phase='inventory', path=str(path))
            entries.append(path)
            if len(entries) > 8192:
                raise RuntimeRefusal('membership_bound', phase='inventory', path=str(root))
    return sorted(entries), walked


def inventory(root, *, collection=None, final=False, directory_epochs=None, auxiliary=None):
    root = Path(root)
    info = root.lstat()
    if root != root.resolve(strict=True) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeRefusal('root_replacement', phase='inventory', path=str(root))
    # Directory modification times change for excluded bytecode; object and security do not.
    security = _security(root, info)
    root_epoch = (info.st_dev, info.st_ino, *security[:4], *security[-2:])
    if directory_epochs is not None:
        _directory_epoch(root, info, security, directory_epochs)
    if auxiliary is not None:
        entries, walked = _request_entries(root, collection)
        auxiliary.update(members={}, directories={})
        entry_types = {}
        for path in entries:
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise RuntimeRefusal('nonregular_member', phase='inventory', path=str(path))
            entry_types[path] = stat.S_IFMT(info.st_mode)
        if {str(root)} | {str(path) for path in entries if entry_types[path] == stat.S_IFDIR} != set(walked):
            raise RuntimeRefusal('incomplete_directory_inventory', phase='inventory', path=str(root))
    else:
        entries = []
        for path in root.rglob('*'):
            if collection is not None:
                collection.check()
            if '__pycache__' not in path.parts:
                entries.append(path)
                if len(entries) > 8192:
                    raise RuntimeRefusal('membership_bound', phase='inventory', path=str(root))
        entries.sort()
    directories = {}
    for path in entries:
        if (entry_types[path] == stat.S_IFDIR if auxiliary is not None else path.is_dir()):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeRefusal('path_replacement', phase='inventory', path=str(path))
            security = _security(path, info)
            name = str(path.relative_to(root))
            value = (info.st_dev, info.st_ino, *security[:4], *security[-2:])
            if auxiliary is not None and '__pycache__' in path.parts:
                auxiliary['directories'][name] = list(value)
            else:
                directories[name] = value
            if directory_epochs is not None:
                _directory_epoch(path, info, security, directory_epochs)
    paths = sorted(path for path in entries if
                   (entry_types[path] == stat.S_IFREG if auxiliary is not None else
                    not _auxiliary_member(path) and (path.is_file() or path.is_symlink())))
    semantic_count = sum(not _auxiliary_member(path) for path in paths)
    if not semantic_count or semantic_count > 4096 or len(paths) > 8192:
        raise RuntimeRefusal('membership_bound', phase='inventory', path=str(root))
    epoch, total = [], 0
    for path in paths:
        if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise RuntimeRefusal('path_replacement', phase='inventory', path=str(path))
        is_auxiliary = _auxiliary_member(path)
        if is_auxiliary and collection is not None:
            collection.strict_members.add(str(path.relative_to(root)))
        observed = file_epoch(path, collection=collection)
        if final and collection is not None:
            observed = collection.final_member(path, observed)
        total += observed[2]
        if observed[2] > 16 * 1024**2 or total > 128 * 1024**2:
            raise RuntimeRefusal('member_size_bound', phase='inventory', path=str(path))
        if is_auxiliary:
            auxiliary['members'][str(path.relative_to(root))] = list(observed)
        else:
            epoch.append((str(path.relative_to(root)), observed))
    if auxiliary is not None and directory_epochs is not None and directory_epochs != walked:
        raise RuntimeRefusal('directory_changed', phase='inventory', path=str(root))
    return root_epoch, tuple(epoch), directories


def complete_security(observed):
    return (observed['root_epoch'][-1] and observed['executable_epoch'][-1]
            and all(value[-1] for value in observed['members'].values())
            and all(value[-1] for value in observed['directories'].values()))


def observation(root, root_epoch, epoch, directories, digest, member_hashes):
    executable = Path(sys.executable).resolve(strict=True)
    return dict(schema=3, member_sha256=member_hashes, root=str(root), root_epoch=list(root_epoch),
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
    if type(value) is dict and value.get('schema') == 3:
        fields = fields | {'member_sha256'}
    if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
            or value['schema'] not in {1, 2, 3} or type(value['members']) is not dict
            or not 1 <= len(value['members']) <= 4096
            or type(value['directories']) is not dict or len(value['directories']) > 4096):
        raise ValueError('Malformed original runtime observation')
    if value['schema'] == 3:
        hashes = value['member_sha256']
        if (type(hashes) is not dict or set(hashes) != set(value['members'])
                or any(type(v) is not str or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
                       for v in hashes.values())):
            raise ValueError('Malformed runtime member content evidence')
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


def _validate_auxiliary(observed, auxiliary):
    if (type(auxiliary) is not dict or set(auxiliary) != {'members', 'directories', 'member_sha256'}
            or any(type(auxiliary[key]) is not dict or len(auxiliary[key]) > 8192 for key in auxiliary)
            or set(auxiliary['member_sha256']) != set(auxiliary['members'])):
        raise ValueError('Incomplete request auxiliary evidence')
    root = Path(observed['root'])
    semantic = set(observed['members']) | set(observed['directories'])
    if (set(auxiliary['members']) & set(auxiliary['directories'])
            or semantic & (set(auxiliary['members']) | set(auxiliary['directories']))
            or len(semantic) + len(auxiliary['members']) + len(auxiliary['directories']) > 8192):
        raise ValueError('Overlapping request member classifications')
    directory_names = set(observed['directories']) | set(auxiliary['directories'])
    for kind, length in (('members', 12), ('directories', 8)):
        for name, epoch in auxiliary[kind].items():
            if type(name) is not str:
                raise ValueError('Malformed auxiliary path')
            path = Path(name)
            if (not name or name == '.' or path.is_absolute() or '..' in path.parts or str(path) != name
                    or not _valid_epoch(epoch, length)
                    or not (stat.S_ISREG(epoch[5]) if kind == 'members' else stat.S_ISDIR(epoch[2]))
                    or not (_auxiliary_member(root / path) if kind == 'members' else
                            '__pycache__' in (root / path).parts)):
                raise ValueError('Malformed auxiliary path or epoch')
            if any(str(parent) not in directory_names for parent in path.parents if parent != Path('.')):
                raise ValueError('Incomplete auxiliary parent evidence')
    for digest in auxiliary['member_sha256'].values():
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Malformed auxiliary content evidence')


def _complete_auxiliary(auxiliary):
    return all(epoch[-1] for kind in ('members', 'directories') for epoch in auxiliary[kind].values())


def _validate_boundary_evidence(record, observed, directories, auxiliary):
    """Validate the entire private token before any provider indexes into it."""
    from .identity import _validate_runtime
    _validate_runtime(record)
    validate_observation(observed)
    _validate_auxiliary(observed, auxiliary)
    if (observed['schema'] != 3 or record['package_sha256'] != observed['package_sha256']
            or record['package_origin'] != observed['root']
            or not Path(record['executable']).is_absolute()
            or record['fingerprint'] != stable_hash({key: value for key, value in record.items()
                if key not in {'package_origin', 'executable', 'fingerprint'}})):
        raise ValueError('Incomplete request runtime identity')
    root = Path(observed['root'])
    expected = {str(root): observed['root_epoch'],
                **{str(root / name): epoch for name, epoch in observed['directories'].items()},
                **{str(root / name): epoch for name, epoch in auxiliary['directories'].items()}}
    if type(directories) is not dict or set(directories) != set(expected):
        raise ValueError('Incomplete request directory evidence')
    for name, epoch in directories.items():
        if (type(epoch) is not tuple or len(epoch) != 10
                or any(type(item) is not int or item < 0 for item in epoch)
                or [*epoch[:5], epoch[9]] != expected[name][:6]):
            raise ValueError('Malformed request directory epoch')


def _request_imports_coherent(observed, auxiliary, *, diagnostics=None):
    """Origins must belong to this request's inventory, including existing cache.

    This is not the source-only predicate for metadata recovery. Bytecode was
    accepted by the full verifier before this request; its private evidence now
    proves it stayed unchanged without altering the enrolled source hash domain.
    """
    root = Path(observed['root'])
    members = set(observed['members']) | set(auxiliary['members'])
    for name, module in tuple(sys.modules.items()):
        if name != 'cmw' and not name.startswith('cmw.'):
            continue
        filename = getattr(module, '__file__', None)
        if filename is None:
            continue
        try:
            path = Path(filename)
            valid = path.is_absolute() and path.is_relative_to(root) and str(path.relative_to(root)) in members
            cached = getattr(module, '__cached__', None)
            if cached is not None:
                cache_path = Path(cached)
                # A nonexistent ordinary cache locator is harmless under -B;
                # any present cache is covered by the complete shared inventory.
                valid = (valid and cache_path.is_absolute() and cache_path.is_relative_to(root)
                         and '..' not in cache_path.parts and _auxiliary_member(cache_path))
                if valid:
                    cache_name = str(cache_path.relative_to(root))
                    present = members | set(observed['directories']) | set(auxiliary['directories'])
                    valid = cache_name not in present or cache_name in auxiliary['members']
        except (TypeError, ValueError):
            valid = False
        if not valid:
            if diagnostics is not None:
                diagnostics.append({'module': name, 'path': str(filename), 'reason': 'outside_request_members'})
            return False
    return True


def _source_imports_coherent(observed, *, diagnostics=None):
    root = Path(observed['root'])
    for name, module in tuple(sys.modules.items()):
        if name != 'cmw' and not name.startswith('cmw.'):
            continue
        filename = getattr(module, '__file__', None)
        if filename is None:
            continue
        path = Path(filename).resolve()
        if not path.is_relative_to(root) or str(path.relative_to(root)) not in observed['members']:
            if diagnostics is not None:
                diagnostics.append({'module': name, 'path': str(path), 'reason': 'outside_observed_members'})
            return False
        cached = getattr(module, '__cached__', None)
        cache_present = bool(cached and Path(cached).exists())
        if path.suffix != '.py' or cache_present:
            if diagnostics is not None:
                diagnostics.append({'module': name, 'path': str(path), 'cached': cached,
                    'cache_present': cache_present, 'reason': 'non_source_or_cached_import'})
            return False
    return True


def _differences(before, after):
    """Original content/authority comparison; raw transition policy is separate."""
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
        if (old[5:8] != new[5:8] or old[9:] != new[9:]
                or (old[8] ^ new[8]) & ~UF_TRACKED):
            return 'security_metadata_changed'
        if old[2:4] != new[2:4]:
            return 'content_epoch_changed'
    if (before['package_sha256'] != after['package_sha256']
            or before.get('member_sha256') != after.get('member_sha256')):
        return 'content_changed'
    return None


class RuntimeVerifier:
    """Immutable enrollment, versioned observation policy, sticky material refusal."""
    def __init__(self, expected, original=None, *, process_started_ns=None, isolated=None):
        self.expected = expected
        self.original = validate_observation(original) if original is not None else None
        self.current = self.original
        self.recovered = False
        self.tainted = False
        self.last_evidence = None
        if process_started_ns is None:
            import psutil
            process_started_ns = int(psutil.Process().create_time() * 1e9)
        self.process_started_ns = process_started_ns
        self.isolated = bool(sys.flags.isolated and sys.dont_write_bytecode) if isolated is None else isolated

    def verify(self, provider):
        if self.tainted:
            raise RuntimeRefusal('fresh_isolated_observer_required', phase='loaded-code',
                                 before=self.last_evidence)
        try:
            return self._verify(provider)
        except RuntimeRefusal as exc:
            exc.evidence['trusted_baseline'] = self.original
            exc.evidence['expected_runtime'] = self.expected
            self.last_evidence = exc.evidence
            self.tainted = True
            raise

    def verify_request(self, provider, boundary_provider, request, *, response=False):
        """Use one full status scan, never reuse trust across requests or mutations."""
        def incomplete():
            raise RuntimeRefusal('incomplete_request_boundary', phase='request-boundary')
        if type(request) is not dict:
            return self.verify(incomplete)
        if response:
            mode = request.pop('mode', None)
            if mode == 'full' and not request:
                return self.verify(provider)
            if mode != 'boundary' or set(request) != {'snapshot'}:
                return self.verify(incomplete)
            snapshot = request.pop('snapshot')
            return self.verify(lambda: self._response_boundary(boundary_provider, snapshot))
        # Older enrollment evidence keeps its exact full-verification policy.
        eligible = (not response and self.original is not None
                    and self.original['schema'] == 3 and complete_security(self.original))
        if not eligible:
            request['mode'] = 'full'
            return self.verify(provider)
        captured = {}
        record = self.verify(lambda: provider(request_boundary=captured))
        if set(captured) != {'observation', 'directory_epochs', 'auxiliary'}:
            # A provider that did not collect the complete request token cannot
            # silently turn a status read into an unverified cached response.
            return self.verify(incomplete)
        request.update(mode='boundary', snapshot={'record': deepcopy(record), **deepcopy(captured)})
        return record

    def _response_boundary(self, provider, snapshot):
        try:
            if type(snapshot) is not dict or set(snapshot) != {'record', 'observation', 'directory_epochs', 'auxiliary'}:
                raise ValueError('Malformed request snapshot')
            _validate_boundary_evidence(snapshot['record'], snapshot['observation'],
                                        snapshot['directory_epochs'], snapshot['auxiliary'])
            if not complete_security(snapshot['observation']) or not _complete_auxiliary(snapshot['auxiliary']):
                raise ValueError('Incomplete request security evidence')
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeRefusal('incomplete_request_boundary', phase='request-boundary') from exc
        before = snapshot['observation']
        observed = None
        try:
            record, observed, directories, auxiliary = provider(snapshot)
            # Both a second full observation and the cheap observation classify
            # membership first. Added files deliberately have no reused digest.
            if (type(auxiliary) is dict and all(type(auxiliary.get(key)) is dict
                                               for key in ('members', 'directories'))
                    and any(set(auxiliary[key]) != set(snapshot['auxiliary'][key])
                            for key in ('members', 'directories'))):
                raise RuntimeRefusal('auxiliary_membership_changed', phase='response-boundary',
                                     before=snapshot['auxiliary'], after=auxiliary)
            _validate_boundary_evidence(record, observed, directories, auxiliary)
        except RuntimeRefusal:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeRefusal('incomplete_observation', phase='response-boundary', after=observed) from exc
        if not complete_security(observed) or not _complete_auxiliary(auxiliary):
            raise RuntimeRefusal('security_metadata_incomplete', phase='response-boundary', after=observed)
        if before != observed or snapshot['directory_epochs'] != directories:
            category = _differences(before, observed)
            raise RuntimeRefusal(category or 'request_epoch_changed', phase='response-boundary',
                                 before=before, after=observed)
        if snapshot['record'] != record:
            raise RuntimeRefusal('interpreter_or_dependency_changed', phase='response-boundary',
                                 before=snapshot['record'], after=record)
        if snapshot['auxiliary'] != auxiliary:
            raise RuntimeRefusal('auxiliary_changed', phase='response-boundary',
                                 before=snapshot['auxiliary'], after=auxiliary)
        if not _request_imports_coherent(observed, auxiliary):
            raise RuntimeRefusal('fresh_isolated_observer_required', phase='loaded-code', after=observed)
        return record, observed

    def _verify(self, provider):
        record, observed = provider()
        try:
            validate_observation(observed)
        except ValueError as exc:
            raise RuntimeRefusal('incomplete_observation', phase='compare', after=observed) from exc
        baseline = self.current
        if baseline is not None and baseline['schema'] < 3:
            # Explicit legacy projection preserves exact matches, not new policy.
            from copy import deepcopy
            observed = deepcopy(observed)
            observed.pop('member_sha256', None)
            observed['schema'] = baseline['schema']
            if baseline['schema'] == 1:
                pairs = [(baseline['root_epoch'], observed['root_epoch']),
                         (baseline['executable_epoch'], observed['executable_epoch'])]
                for key in ('members', 'directories'):
                    pairs.extend((epoch, observed[key][name]) for name, epoch in baseline[key].items()
                                 if name in observed[key])
                for old, new in pairs:
                    if not old[-1]:
                        new[-1] = False
        evidence = dict(before=baseline, after=observed,
            expected_digest=self.expected['package_sha256'], actual_digest=record['package_sha256'])
        if record['fingerprint'] != self.expected['fingerprint']:
            category = _differences(baseline, observed) if baseline is not None else None
            raise RuntimeRefusal(category or 'runtime_fingerprint_changed', phase='compare', **evidence)
        if baseline is None:
            self.current = observed
            return record
        if baseline == observed:
            return record
        if self.original is None:
            raise RuntimeRefusal('original_baseline_unavailable', phase='compare', **evidence)
        category = _differences(self.original, observed)
        if category is None and baseline['schema'] < 3:
            category = 'legacy_observation_policy_requires_enrollment'
        if category is None and (not baseline['root_epoch'][-1] or
                not baseline['executable_epoch'][-1] or
                not all(epoch[-1] for epoch in baseline['members'].values()) or
                not all(epoch[-1] for epoch in baseline['directories'].values())):
            category = 'security_baseline_incomplete'
        if category is None:
            for name, old in baseline['members'].items():
                category = _member_difference(old, observed['members'][name])
                if category:
                    break
        if category:
            raise RuntimeRefusal(category, phase='compare', **evidence)
        changed = [name for name, new in observed['members'].items()
                   if new != baseline['members'][name]]
        imports = []
        imports_coherent = _source_imports_coherent(observed, diagnostics=imports) if self.isolated else None
        predicate = ('not_isolated' if not self.isolated else
                     'source_imports_incoherent' if not imports_coherent else None)
        if predicate:
            refused = RuntimeRefusal('fresh_isolated_observer_required', phase='loaded-code', **evidence)
            refused.evidence['loaded_code'] = {
                'failed_predicate': predicate, 'isolated': self.isolated,
                'process_started_ns': self.process_started_ns,
                'latest_changed_ctime_ns': max((observed['members'][name][4] for name in changed), default=None),
                'source_imports_coherent': imports_coherent, 'import_failures': imports,
                'changed_members': changed, 'policy': 3}
            raise refused
        self.current = observed
        self.recovered = True
        self.last_evidence = {**evidence, 'category': 'uf_tracked_current_disk_verified',
                              'policy': 3, 'changed_members': changed}
        return record
