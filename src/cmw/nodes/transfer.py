"""Selected-file manifests, private SFTP staging and no-clobber local publication.

Transfer lineage is independent of Jobs insertion and scientific execution. A
published bundle alone is never a runnable job, an execution receipt or a result.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import stat
import subprocess
import time
import unicodedata
from uuid import uuid4

from cmw.core.preparation_publication import _rename_exclusive, _sync_directory
from cmw.core.provenance import atomic_write_json, canonical_json_bytes, stable_hash
from cmw.core.storage import BYTES_PER_GIB, check_storage_capacity
from .protocol import NodeError, decode, identifier
from .storage import check_storage


MAX_FILES = 128
MAX_FILE_BYTES = 64 * 1024**2
MAX_TOTAL_BYTES = 256 * 1024**2
MAX_MANIFEST_BYTES = 128 * 1024
_SPACE_MARGIN = 16 * 1024**2
_FILE_FIELDS = {'path', 'role', 'bytes', 'sha256', 'origin', 'mode'}
_MANIFEST_FIELDS = {'schema', 'node_id', 'queue_id', 'files', 'potcar_authorized'}
_FORBIDDEN_NAMES = {'vasp_std', 'vasp_gam', 'vasp_ncl', 'orca', 'multiwfn',
                    'id_rsa', 'id_ed25519', 'authorized_keys', 'known_hosts'}


def _error(code, message):
    raise NodeError(code, message)


def _path(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in value) or '\\' in value
            or unicodedata.normalize('NFC', value) != value):
        _error('INVALID_MANIFEST', 'File paths must be bounded NFC POSIX paths without control characters')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or any(part in {'', '.', '..'} for part in value.split('/'))
            or len(path.parts) < 2 or path.parts[0] not in {'inputs', 'support'}):
        _error('INVALID_MANIFEST', 'Each file must be an explicit relative inputs/ or support/ path')
    return path


def normalize_manifest(value):
    """Validate one bounded portable manifest; no filesystem access occurs."""
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS or type(value['schema']) is not int or value['schema'] != 1:
        _error('INVALID_MANIFEST', 'Expected an exact schema-1 transfer manifest')
    identifier(value['node_id'], 'target node')
    identifier(value['queue_id'], 'target queue')
    if type(value['potcar_authorized']) is not bool:
        _error('INVALID_MANIFEST', 'POTCAR transfer authorization must be explicit boolean data')
    files = value['files']
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        _error('TRANSFER_LIMIT', 'Select between 1 and 128 regular files explicitly; directory recursion is unsupported')
    normalized, names, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            _error('INVALID_MANIFEST', 'Each file requires path, role, bytes, sha256, origin and mode')
        path = _path(item['path'])
        folded = str(path).casefold()
        if folded in names or any(folded.startswith(old + '/') or old.startswith(folded + '/') for old in names):
            _error('INVALID_MANIFEST', 'Duplicate, case-folded or file/directory destinations are ambiguous')
        names.add(folded)
        if not isinstance(item['role'], str) or item['role'] not in {'input', 'support', 'potcar'}:
            _error('INVALID_MANIFEST', 'Only input, support and explicitly authorized potcar roles are supported')
        if (item['role'] == 'support') != (path.parts[0] == 'support'):
            _error('INVALID_MANIFEST', 'Supporting files must be separate from the input bundle')
        if type(item['mode']) is not int or item['mode'] not in ({0o600, 0o700} if item['role'] == 'support' else {0o600}):
            _error('INVALID_MANIFEST', 'Only private data mode 0600 or explicitly executable support mode 0700 is allowed')
        if type(item['bytes']) is not int or not 0 <= item['bytes'] <= MAX_FILE_BYTES:
            _error('TRANSFER_LIMIT', 'The MVP permits at most 64 MiB per selected file; large restart transfer is unsupported')
        total += item['bytes']
        if not isinstance(item['sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None:
            _error('INVALID_MANIFEST', 'Each file requires an exact lowercase SHA-256 digest')
        if (not isinstance(item['origin'], str) or not item['origin'] or len(item['origin']) > 4096
                or any(ord(char) < 32 for char in item['origin'])):
            _error('INVALID_MANIFEST', 'Each file requires a bounded explicit origin locator')
        basename = path.name.casefold()
        if basename.startswith(('wavecar', 'chgcar')):
            _error('UNSUPPORTED_RESTART', 'WAVECAR/CHGCAR transfer is not qualified; the supported runner requires restart none')
        if basename in _FORBIDDEN_NAMES or '.ssh' in {part.casefold() for part in path.parts}:
            _error('FORBIDDEN_TRANSFER', 'Engines, credentials and SSH state are not transferable inputs')
        if basename.startswith('potcar') or item['role'] == 'potcar':
            if path.name != 'POTCAR' or item['role'] != 'potcar' or not value['potcar_authorized']:
                _error('POTCAR_AUTHORIZATION_REQUIRED', 'Exact POTCAR transfer requires its potcar role and explicit authorization')
        normalized.append(dict(item))
    if total > MAX_TOTAL_BYTES:
        _error('TRANSFER_LIMIT', 'The MVP permits at most 256 MiB per explicitly selected bundle')
    result = {**value, 'files': sorted(normalized, key=lambda item: item['path'])}
    if len(canonical_json_bytes(result)) > MAX_MANIFEST_BYTES:
        _error('TRANSFER_LIMIT', 'Transfer manifest exceeds 128 KiB')
    return result


def manifest_digest(manifest):
    return stable_hash(normalize_manifest(manifest))


def _direct(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        _error('UNSAFE_TRANSFER_PATH', 'Transfer paths must be canonical without symlink traversal')
    info = path.lstat()
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode):
        _error('UNSAFE_TRANSFER_PATH', 'Only selected regular files and owned directories are supported')
    return path


def _private(path, *, directory=False):
    path = _direct(path, directory=directory)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        _error('UNSAFE_TRANSFER_PATH', 'Transfer staging must remain private to its owner')
    return path


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _directory_id(path):
    info = _direct(path, directory=True).stat()
    return [info.st_dev, info.st_ino]


def _read_json(path):
    path = _private(path)
    with path.open('rb') as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 64 * 1024 + 1)
    if len(raw) > MAX_MANIFEST_BYTES + 64 * 1024:
        _error('INVALID_TRANSFER_RECORD', 'Transfer metadata exceeds its bound')
    return decode(raw if raw.endswith(b'\n') else raw + b'\n')


def _write_json(path, value):
    _private(path.parent, directory=True)
    if os.path.lexists(path):
        _private(path)
    atomic_write_json(path, value)
    _sync_directory(path.parent)


def _scan_file(path, *, destination=None, expected=None, mode=0o600):
    """Hash/copy the same bounded opened bytes and reject a changed source version."""
    path = _direct(path)
    before = path.lstat()
    if before.st_size > MAX_FILE_BYTES:
        _error('TRANSFER_LIMIT', 'A selected file exceeds the 64 MiB MVP bound')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    output = None
    try:
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if _identity(opened) != _identity(before) or not stat.S_ISREG(opened.st_mode):
                _error('SOURCE_CHANGED', 'Selected source changed while opening')
            if destination is not None:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
                output = os.fdopen(fd, 'wb')
            digest, count = hashlib.sha256(), 0
            while True:
                block = source.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - count))
                if not block:
                    break
                count += len(block)
                if count > MAX_FILE_BYTES:
                    _error('SOURCE_CHANGED', 'Selected source grew beyond the transfer bound')
                digest.update(block)
                if output is not None:
                    output.write(block)
            after = os.fstat(source.fileno())
            if (_identity(opened) != _identity(after) or _identity(opened) != _identity(path.lstat())
                    or path != path.resolve(strict=True) or count != opened.st_size):
                _error('SOURCE_CHANGED', 'Selected source changed while copying or hashing')
            identity = {'bytes': count, 'sha256': digest.hexdigest()}
            if expected is not None and identity != {key: expected[key] for key in identity}:
                _error('DIGEST_MISMATCH', 'Selected file differs from its exact manifest version')
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            return {**identity, 'source_identity': _identity(opened)}
    finally:
        if output is not None:
            output.close()


def prepare_local_bundle(spool, selections, *, node_id, queue_id, potcar_authorized=False):
    """Copy explicit stable source versions to a new private flat upload spool."""
    identifier(node_id, 'target node')
    identifier(queue_id, 'target queue')
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
        _error('TRANSFER_LIMIT', 'Explicitly select 1..128 files')
    prepared = []
    for selection in selections:
        if (not isinstance(selection, dict) or not {'source', 'path', 'role'} <= set(selection)
                or set(selection) - {'source', 'path', 'role', 'mode', 'origin'}):
            _error('INVALID_MANIFEST', 'Selection requires source, relative path and role')
        source = _direct(selection['source'])
        basename = source.name.casefold()
        if basename in _FORBIDDEN_NAMES or basename.startswith(('wavecar', 'chgcar')):
            _error('FORBIDDEN_TRANSFER', 'Renaming an engine, credential or unsupported restart file does not authorize transfer')
        if basename.startswith('potcar') and (selection['role'] != 'potcar' or not potcar_authorized):
            _error('POTCAR_AUTHORIZATION_REQUIRED', 'Selected POTCAR bytes require explicit authorized transfer')
        # Validate path/role/licensing before creating any spool data.
        prepared.append({'path': selection['path'], 'role': selection['role'], 'bytes': source.stat().st_size,
                         'sha256': '0' * 64, 'origin': selection.get('origin', str(source)),
                         'mode': selection.get('mode', 0o600), 'source': source})
    probe = {'schema': 1, 'node_id': node_id, 'queue_id': queue_id,
             'potcar_authorized': potcar_authorized,
             'files': [{key: value for key, value in item.items() if key != 'source'} for item in prepared]}
    normalize_manifest(probe)
    spool = Path(spool)
    if not spool.is_absolute() or spool != spool.resolve():
        _error('UNSAFE_TRANSFER_PATH', 'Spool must be a new canonical absolute directory')
    _direct(spool.parent, directory=True)
    spool.mkdir(mode=0o700)
    sources, files = [], []
    for index, item in enumerate(sorted(prepared, key=lambda item: item['path'])):
        flat = f'{index:04d}.bin'
        observed = _scan_file(item['source'], destination=spool / flat)
        files.append({**{key: value for key, value in item.items() if key != 'source'},
                      'bytes': observed['bytes'], 'sha256': observed['sha256']})
        sources.append({'source': str(item['source']), 'identity': observed['source_identity'], 'flat': flat})
    manifest = normalize_manifest({**probe, 'files': files})
    record = {'schema': 1, 'manifest': manifest, 'manifest_digest': manifest_digest(manifest),
              'sources': sources, 'spool_identity': _directory_id(spool)}
    _write_json(spool / 'spool.json', record)
    _sync_directory(spool)
    return {'spool': str(spool), 'manifest': manifest, 'manifest_digest': record['manifest_digest']}


def _spool(spool):
    spool = _private(Path(spool), directory=True)
    record = _read_json(spool / 'spool.json')
    if set(record) != {'schema', 'manifest', 'manifest_digest', 'sources', 'spool_identity'} or record['schema'] != 1:
        _error('INVALID_TRANSFER_RECORD', 'Malformed local spool metadata')
    manifest = normalize_manifest(record['manifest'])
    if manifest_digest(manifest) != record['manifest_digest'] or _directory_id(spool) != record['spool_identity']:
        _error('SOURCE_CHANGED', 'Local spool identity changed')
    if not isinstance(record['sources'], list) or len(record['sources']) != len(manifest['files']):
        _error('INVALID_TRANSFER_RECORD', 'Spool source list is inconsistent')
    expected_names = {'spool.json'}
    for index, (file, source) in enumerate(zip(manifest['files'], record['sources'])):
        flat = f'{index:04d}.bin'
        if set(source) != {'source', 'identity', 'flat'} or source['flat'] != flat:
            _error('INVALID_TRANSFER_RECORD', 'Malformed spool source identity')
        original = _direct(source['source'])
        if _identity(original.stat()) != source['identity']:
            _error('SOURCE_CHANGED', 'Original selected source changed; prepare a new reviewed manifest')
        _scan_file(spool / flat, expected=file)
        expected_names.add(flat)
    if {path.name for path in spool.iterdir()} != expected_names:
        _error('UNSAFE_TRANSFER_PATH', 'Unexpected files appeared in the private spool')
    return spool, record


def _layout(config, storage_alias, *, create=False):
    identifier(config['node_id'], 'node identity')
    try:
        storage = config['storages'][storage_alias]
    except KeyError as exc:
        raise NodeError('UNKNOWN_STORAGE', 'Storage alias is not explicitly enrolled') from exc
    ready = check_storage(storage)
    if not ready['ready']:
        _error('STORAGE_UNAVAILABLE', ready['error'])
    root = Path(storage['root']) / ('cmw-' + config['node_id'])
    if not root.exists():
        if not create:
            return None
        root.mkdir(mode=0o700)
        children = {}
        for name in ('incoming', 'prepared', 'records', 'locks'):
            (root / name).mkdir(mode=0o700)
            children[name] = _directory_id(root / name)
        owner = {'schema': 1, 'node_id': config['node_id'], 'storage_id': storage['storage_id'],
                 'identity': _directory_id(root), 'children': children}
        _write_json(root / 'owner.json', owner)
        _sync_directory(root.parent)
    _private(root, directory=True)
    owner = _read_json(root / 'owner.json')
    if (set(owner) != {'schema', 'node_id', 'storage_id', 'identity', 'children'} or owner['schema'] != 1
            or owner['node_id'] != config['node_id'] or owner['storage_id'] != storage['storage_id']
            or owner['identity'] != _directory_id(root)
            or set(owner['children']) != {'incoming', 'prepared', 'records', 'locks'}):
        _error('STORAGE_CHANGED', 'Transfer namespace ownership changed')
    for name, identity in owner['children'].items():
        if _directory_id(_private(root / name, directory=True)) != identity:
            _error('STORAGE_CHANGED', 'Transfer publication parent was replaced')
    return root


@contextmanager
def _lock(root, request_id):
    path = root / 'locks' / (identifier(request_id, 'request ID') + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _error('UNSAFE_TRANSFER_PATH', 'Transfer lock ownership changed')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NodeError('TRANSFER_BUSY', 'Another request is updating this transfer; inspect then retry the same ID') from exc
        yield
    finally:
        os.close(fd)


def _record_path(root, request_id):
    return root / 'records' / (identifier(request_id, 'request ID') + '.json')


def _load(root, request_id):
    record = _read_json(_record_path(root, request_id))
    expected = {'schema', 'node_id', 'queue_id', 'storage_id', 'request_id', 'manifest_digest',
                'manifest', 'status', 'incoming_path', 'published_root', 'namespace_root',
                'owner_token', 'incoming_identity', 'candidates', 'incoming_cleaned'}
    if (set(record) != expected or type(record['schema']) is not int or record['schema'] != 1
            or record['request_id'] != request_id or record['namespace_root'] != str(root)
            or record['incoming_path'] != str(root / 'incoming' / request_id / 'upload')
            or record['published_root'] != str(root / 'prepared' / request_id)
            or record['manifest_digest'] != manifest_digest(record['manifest'])
            or root.name != 'cmw-' + record['node_id']
            or type(record['incoming_cleaned']) is not bool
            or record['node_id'] != record['manifest']['node_id']
            or record['queue_id'] != record['manifest']['queue_id']
            or record['status'] not in {'INITIALIZING', 'INCOMING', 'PUBLISHING', 'PUBLISHED'}):
        _error('INVALID_TRANSFER_RECORD', 'Transfer ledger contradicts its manifest or owned locations')
    identifier(record['owner_token'], 'transfer ownership token')
    candidates = record['candidates']
    if not isinstance(candidates, list) or len(candidates) > 8:
        _error('INVALID_TRANSFER_RECORD', 'Malformed publication candidate identities')
    parent = Path(record['incoming_path']).parent
    for candidate in candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {'path', 'identity'}
                or not isinstance(candidate['path'], str)
                or Path(candidate['path']).parent != parent
                or re.fullmatch(r'candidate-[0-9a-f]{32}', Path(candidate['path']).name) is None
                or not isinstance(candidate['identity'], list) or len(candidate['identity']) != 2
                or any(type(value) is not int or value < 0 for value in candidate['identity'])):
            _error('INVALID_TRANSFER_RECORD', 'Malformed publication candidate identity')
    return record


def _incoming(record, *, create=False):
    upload = Path(record['incoming_path'])
    parent = upload.parent
    owner = {'schema': 1, 'request_id': record['request_id'], 'owner_token': record['owner_token'],
             'manifest_digest': record['manifest_digest']}
    if not parent.exists() and create:
        parent.mkdir(mode=0o700)
        upload.mkdir(mode=0o700)
        _write_json(parent / 'owner.json', owner)
    _private(parent, directory=True)
    _private(upload, directory=True)
    if _read_json(parent / 'owner.json') != owner:
        _error('UNSAFE_TRANSFER_PATH', 'Incoming directory ownership changed')
    identity = _directory_id(parent)
    if record['incoming_identity'] is not None and identity != record['incoming_identity']:
        _error('UNSAFE_TRANSFER_PATH', 'Incoming directory was replaced')
    return upload, identity


def _receipt(record):
    return {key: record[key] for key in ('schema', 'node_id', 'queue_id', 'storage_id', 'request_id',
            'manifest_digest', 'manifest', 'status', 'incoming_path', 'published_root', 'namespace_root')}


def _space(root, manifest):
    # Uploaded bytes and one complete publication candidate coexist. This is a
    # current capacity check, not a filesystem reservation against other users.
    needed = 2 * sum(item['bytes'] for item in manifest['files']) + _SPACE_MARGIN
    return check_storage_capacity(root, minimum_free_gb=needed / BYTES_PER_GIB)


def begin_transfer(config, storage_alias, request_id, manifest):
    manifest = normalize_manifest(manifest)
    identifier(request_id, 'request ID')
    if (manifest['node_id'] != config['node_id'] or manifest['queue_id'] not in
            {queue['queue_id'] for queue in config['queues'].values()}):
        _error('WRONG_NODE', 'Manifest belongs to a different node or enrolled queue')
    digest = manifest_digest(manifest)
    root = _layout(config, storage_alias, create=True)
    with _lock(root, request_id):
        path = _record_path(root, request_id)
        if path.exists():
            record = _load(root, request_id)
            if record['manifest_digest'] != digest:
                _error('REQUEST_CONFLICT', 'Request ID already names a different manifest; never retry with changed inputs')
            if record['status'] == 'PUBLISHED':
                _verify_published(record)
                return _receipt(record)
        else:
            _space(root, manifest)
            record = {'schema': 1, 'node_id': config['node_id'], 'queue_id': manifest['queue_id'],
                      'storage_id': config['storages'][storage_alias]['storage_id'], 'request_id': request_id,
                      'manifest_digest': digest, 'manifest': manifest, 'status': 'INITIALIZING',
                      'incoming_path': str(root / 'incoming' / request_id / 'upload'),
                      'published_root': str(root / 'prepared' / request_id), 'namespace_root': str(root),
                      'owner_token': uuid4().hex, 'incoming_identity': None, 'candidates': [], 'incoming_cleaned': False}
            _write_json(path, record)
        _, record['incoming_identity'] = _incoming(record, create=True)
        if record['status'] == 'INITIALIZING':
            record['status'] = 'INCOMING'
            _write_json(path, record)
        return _receipt(record)


@lru_cache(maxsize=256)
def _verify_version(path, epoch, digest, size):
    # This cache grants no cross-node identity. Every call first rechecks all
    # target-local path/stat evidence, including ctime and replacement inode.
    observed = _scan_file(Path(path), expected={'sha256': digest, 'bytes': size})
    if tuple(observed['source_identity']) != epoch:
        _error('SOURCE_CHANGED', 'Published source changed while validating its version')


def _verify_published(record):
    root = _private(Path(record['published_root']), directory=True)
    # Same-volume promotion preserves the candidate directory identity. Equal
    # bytes in an independently copied tree do not prove our publication won.
    if not record['candidates'] or _directory_id(root) != record['candidates'][-1]['identity']:
        _error('PUBLICATION_CONFLICT', 'Published directory is not the recorded promotion candidate')
    expected = {file['path'] for file in record['manifest']['files']}
    expected_directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents
                            if str(parent) != '.'}
    observed, directories = set(), set()
    for path in root.rglob('*'):
        _direct(path, directory=path.is_dir())
        relative = str(path.relative_to(root))
        if path.is_file():
            observed.add(relative)
        else:
            _private(path, directory=True)
            directories.add(relative)
    if observed != expected or directories != expected_directories:
        _error('DIGEST_MISMATCH', 'Published bundle has missing or unexpected files/directories')
    for file in record['manifest']['files']:
        path = _direct(root / file['path'])
        epoch = tuple(_identity(path.stat()))
        _verify_version(str(path), epoch, file['sha256'], file['bytes'])
        if tuple(_identity(path.stat())) != epoch or path != path.resolve(strict=True):
            _error('SOURCE_CHANGED', 'Published source changed during verification')
        if stat.S_IMODE(path.stat().st_mode) != file['mode']:
            _error('DIGEST_MISMATCH', 'Published file mode differs from its reviewed manifest')
    return root


def lookup_transfer(config, storage_alias, request_id):
    identifier(request_id, 'request ID')
    root = _layout(config, storage_alias)
    if root is None or not _record_path(root, request_id).exists():
        return {'status': 'NOT_FOUND', 'request_id': request_id}
    record = _load(root, request_id)
    if record['status'] == 'PUBLISHED':
        _verify_published(record)
    return _receipt(record)


def publish_transfer(config, storage_alias, request_id, digest, *, _fault=None):
    root = _layout(config, storage_alias)
    if root is None:
        _error('TRANSFER_NOT_FOUND', 'Begin this request before publication')
    with _lock(root, request_id):
        record = _load(root, request_id)
        if digest != record['manifest_digest']:
            _error('REQUEST_CONFLICT', 'Publication digest differs from the original manifest')
        if record['status'] == 'PUBLISHED':
            _verify_published(record)
            return _receipt(record)
        target = Path(record['published_root'])
        if target.exists():
            if record['status'] != 'PUBLISHING':
                _error('PUBLICATION_CONFLICT', 'Unrelated destination already exists')
            _verify_published(record)
            _layout(config, storage_alias)
            record['status'] = 'PUBLISHED'
            _write_json(_record_path(root, request_id), record)
            return _receipt(record)
        upload, _ = _incoming(record)
        files = record['manifest']['files']
        if {path.name for path in upload.iterdir()} != {f'{index:04d}.bin' for index in range(len(files))}:
            _error('INCOMPLETE_TRANSFER', 'Uploaded files are incomplete or contain unexpected entries')
        _space(root, record['manifest'])
        if len(record['candidates']) >= 8:
            _error('TRANSFER_RECOVERY_REQUIRED', 'Eight interrupted publication candidates retained; inspect owned staging before recovery')
        candidate = upload.parent / ('candidate-' + uuid4().hex)
        candidate.mkdir(mode=0o700)
        record['candidates'].append({'path': str(candidate), 'identity': _directory_id(candidate)})
        record['status'] = 'PUBLISHING'
        _write_json(_record_path(root, request_id), record)
        for index, file in enumerate(files):
            destination = candidate / file['path']
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _scan_file(upload / f'{index:04d}.bin', destination=destination, expected=file, mode=file['mode'])
        for directory in sorted((path for path in candidate.rglob('*') if path.is_dir()), reverse=True):
            _sync_directory(directory)
        _sync_directory(candidate)
        if _fault:
            _fault('before_publish')
        _layout(config, storage_alias)  # Recheck volume and publication parents immediately before rename.
        _rename_exclusive(candidate, target)
        _sync_directory(target.parent)
        if _fault:
            _fault('after_publish')
        _verify_published(record)
        _layout(config, storage_alias)
        record['status'] = 'PUBLISHED'
        _write_json(_record_path(root, request_id), record)
        if _fault:
            _fault('after_record')
        return _receipt(record)



def cleanup_transfer(config, storage_alias, request_id):
    """Remove only verified request-owned staging after publication; retain ledger."""
    root = _layout(config, storage_alias)
    if root is None:
        _error('TRANSFER_NOT_FOUND', 'No transfer namespace exists')
    with _lock(root, request_id):
        record = _load(root, request_id)
        if record['status'] != 'PUBLISHED':
            _error('TRANSFER_NOT_PUBLISHED', 'Unpublished transfer evidence is retained for inspection')
        _verify_published(record)
        parent = Path(record['incoming_path']).parent
        if not parent.exists():
            record['incoming_cleaned'] = True
            _write_json(_record_path(root, request_id), record)
            return {'status': 'CLEANED', 'request_id': request_id}
        upload, _ = _incoming(record)
        candidates = record['candidates']
        expected_names = {'owner.json', 'upload'} | {Path(item['path']).name for item in candidates}
        if {path.name for path in parent.iterdir()} - expected_names:
            _error('UNSAFE_TRANSFER_PATH', 'Unexpected staging entries prevent automatic cleanup')
        trees = [upload]
        for candidate in candidates:
            path = Path(candidate['path'])
            if path.parent != parent or not path.name.startswith('candidate-'):
                _error('INVALID_TRANSFER_RECORD', 'Unsafe candidate cleanup path')
            if os.path.lexists(path):
                _private(path, directory=True)
                if _directory_id(path) != candidate['identity']:
                    _error('UNSAFE_TRANSFER_PATH', 'Candidate ownership changed; cleanup refused')
                trees.append(path)
        flat = {f'{index:04d}.bin' for index in range(len(record['manifest']['files']))}
        allowed = {file['path'] for file in record['manifest']['files']}
        for tree in trees:
            selected = flat if tree == upload else allowed
            for path in tree.rglob('*'):
                _private(path, directory=path.is_dir())
                if path.is_file() and str(path.relative_to(tree)) not in selected:
                    _error('UNSAFE_TRANSFER_PATH', 'Unselected staging file prevents automatic cleanup')
        # Ownership and all descendants were checked before the first deletion.
        for tree in trees:
            shutil.rmtree(tree)
        (parent / 'owner.json').unlink()
        parent.rmdir()
        _sync_directory(parent.parent)
        record['incoming_cleaned'] = True
        _write_json(_record_path(root, request_id), record)
        return {'status': 'CLEANED', 'request_id': request_id}


def _upload_receipt(record, receipt):
    fields = {'schema', 'node_id', 'queue_id', 'storage_id', 'request_id', 'manifest_digest',
              'manifest', 'status', 'incoming_path', 'published_root', 'namespace_root'}
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt['schema'] != 1:
        _error('INVALID_RESPONSE', 'Malformed transfer receipt')
    identifier(receipt['request_id'], 'request ID')
    namespace = Path(receipt['namespace_root'])
    if (receipt['manifest_digest'] != record['manifest_digest'] or receipt['manifest'] != record['manifest']
            or receipt['node_id'] != record['manifest']['node_id'] or receipt['queue_id'] != record['manifest']['queue_id']
            or not namespace.is_absolute() or namespace.name != 'cmw-' + receipt['node_id']
            or receipt['incoming_path'] != str(namespace / 'incoming' / receipt['request_id'] / 'upload')
            or receipt['published_root'] != str(namespace / 'prepared' / receipt['request_id'])
            or receipt['status'] not in {'INCOMING', 'PUBLISHING'}):
        _error('INVALID_RESPONSE', 'Transfer receipt does not name this manifest and owned incoming location')


def upload_local_bundle(spool, receipt):
    """Same-host transport for explicitly local nodes and isolated acceptance."""
    spool, record = _spool(spool)
    _upload_receipt(record, receipt)
    root = Path(receipt['namespace_root'])
    server = _load(root, receipt['request_id'])
    if _receipt(server) != receipt:
        _error('INVALID_RESPONSE', 'Local destination receipt changed')
    upload, _ = _incoming(server)
    for index, file in enumerate(record['manifest']['files']):
        name = f'{index:04d}.bin'
        temporary = upload / ('.upload-' + uuid4().hex)
        try:
            _scan_file(spool / name, destination=temporary, expected=file)
            if os.path.lexists(upload / name):
                _private(upload / name)
            os.replace(temporary, upload / name)
        finally:
            if temporary.exists():
                temporary.unlink()
    _sync_directory(upload)
    _spool(spool)  # A changed original source never proceeds to publication.
    return {'status': 'UPLOADED', 'manifest_digest': record['manifest_digest']}


def sftp_quote(value, *, glob=False):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _error('UNSAFE_TRANSFER_PATH', 'SFTP paths cannot contain control characters')
    # put's destination is literal; get's source is a glob. Both parse quoted
    # spaces and escaped quotes/backslashes before the selected path operation.
    escaped = '\\"' + ('*?[]' if glob else '')
    return '"' + ''.join('\\' + char if char in escaped else char for char in value) + '"'


def sftp_command(connection):
    alias, key = connection.get('ssh_alias'), connection.get('ssh_host_key')
    if not isinstance(alias, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias) is None:
        _error('INVALID_CONNECTION', 'SFTP requires an explicitly enrolled SSH alias')
    if not isinstance(key, str) or re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', key) is None:
        _error('INVALID_CONNECTION', 'SFTP requires the enrolled SSH host-key fingerprint')
    if not os.access('/usr/bin/sftp', os.X_OK):
        _error('SFTP_UNAVAILABLE', 'Installed OpenSSH sftp is required; no scp fallback is used')
    return ['/usr/bin/sftp', '-v', '-b', '-', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ConnectTimeout=5',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1', '--', alias]


def run_sftp(argv, batch, *, cwd, host_key, timeout=120):
    """Bound all pipes and verify this SSH channel before sending any batch data."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        _error('INVALID_CONNECTION', 'SFTP timeout must be within 300 seconds')
    if not isinstance(batch, bytes) or len(batch) > 128 * 1024:
        _error('TRANSFER_LIMIT', 'SFTP batch exceeds its bound')
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    output, diagnostic, line_buffer = bytearray(), bytearray(), bytearray()
    verified, sent, deadline = host_key is None, 0, time.monotonic() + timeout
    try:
        for handle in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(handle.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'out')
            selector.register(process.stderr, selectors.EVENT_READ, 'err')
            input_registered = False
            while selector.get_map() or process.poll() is None:
                if verified and not input_registered and not process.stdin.closed:
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'in')
                    input_registered = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _error('TRANSFER_INTERRUPTED', 'SFTP timed out; incoming bytes remain unpublished; retry the same manifest')
                for item, _ in selector.select(min(remaining, 1)):
                    if item.data == 'in':
                        sent += os.write(item.fd, batch[sent:])
                        if sent == len(batch):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(item.fd, 65536)
                        if not data:
                            selector.unregister(item.fileobj)
                            continue
                        target = diagnostic if item.data == 'err' else output
                        target.extend(data)
                        del target[:-65536]
                        if item.data == 'err':
                            line_buffer.extend(data)
                            if len(line_buffer) > 65536:
                                _error('INVALID_RESPONSE', 'SFTP diagnostic line exceeded its bound')
                            while b'\n' in line_buffer:
                                line, _, rest = line_buffer.partition(b'\n')
                                line_buffer[:] = rest
                                match = re.search(rb'Server host key: \S+ (SHA256:[A-Za-z0-9+/]+)', line)
                                if match and host_key is not None:
                                    if match.group(1).decode() != host_key:
                                        _error('SSH_IDENTITY_CHANGED', 'SFTP server host key differs from enrollment')
                                    verified = True
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if not verified:
                _error('SSH_IDENTITY_UNVERIFIED', 'SFTP channel did not establish its enrolled SSH host key')
            if code:
                _error('TRANSFER_INTERRUPTED', 'SFTP failed; incoming bytes remain unpublished: ' + diagnostic.decode(errors='replace')[-2048:])
            return {'exit_code': 0, 'diagnostics': diagnostic.decode(errors='replace')[-2048:]}
    finally:
        for handle in (process.stdin, process.stdout, process.stderr):
            if not handle.closed:
                handle.close()
        if process.poll() is None:
            process.terminate()  # Only this client-owned transfer process.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def upload_sftp(connection, spool, receipt, *, _argv=None, _host_key=None, timeout=120):
    """Upload only generated flat names through a separate installed SFTP channel."""
    spool, record = _spool(spool)
    _upload_receipt(record, receipt)
    commands = [f'put {index:04d}.bin {sftp_quote(str(Path(receipt["incoming_path"]) / f"{index:04d}.bin"))}'
                for index in range(len(record['manifest']['files']))]
    argv = sftp_command(connection) if _argv is None else _argv
    host_key = connection['ssh_host_key'] if _argv is None else _host_key
    result = run_sftp(argv, ('\n'.join(commands) + '\nbye\n').encode(), cwd=spool, host_key=host_key, timeout=timeout)
    _spool(spool)
    return {'status': 'UPLOADED', 'manifest_digest': record['manifest_digest'], **result}
