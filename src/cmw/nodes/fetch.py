"""Explicit declared-file captures and verified replicas, never relocated originals."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
from uuid import uuid4

from cmw.core.preparation_publication import _rename_exclusive, _sync_directory
from cmw.core.provenance import stable_hash
from cmw.core.storage import BYTES_PER_GIB, check_storage_capacity
from cmw.jobs.locations import discover
from cmw.jobs.store import TERMINAL
from .protocol import NodeError, identifier, target_identity
from .storage import check_storage
from .transfer import (_direct, _private, _directory_id, _identity, _read_json,
                       _write_json, _layout, _lock, run_sftp, sftp_command, sftp_quote)

MAX_FILES = 128
NORMAL_FILE_BYTES, NORMAL_TOTAL_BYTES = 64 * 1024**2, 256 * 1024**2
LARGE_FILE_BYTES, LARGE_TOTAL_BYTES = 2 * 1024**3, 4 * 1024**3
_MARGIN = 16 * 1024**2
_MANIFEST_NAME = 'fetch-manifest.json'
_PROHIBITED = {'vasp_std', 'vasp_gam', 'vasp_ncl', 'orca', 'multiwfn',
               'id_rsa', 'id_ed25519', 'authorized_keys', 'known_hosts'}


def _require(condition, message, code='INVALID_FETCH'):
    if not condition:
        raise NodeError(code, message)


def _relative(value, *, empty=False):
    _require(isinstance(value, str) and len(value.encode('utf-8')) <= 1024
             and all(ord(char) >= 32 and ord(char) != 127 for char in value)
             and '\\' not in value and unicodedata.normalize('NFC', value) == value,
             'Fetch paths must be bounded NFC relative POSIX paths')
    if empty and value == '':
        return value
    path = PurePosixPath(value)
    _require(bool(value) and not path.is_absolute() and str(path) == value
             and not any(part in {'', '.', '..'} for part in value.split('/')),
             'Fetch paths cannot escape the declared location or replica')
    _require(path.name.casefold() not in _PROHIBITED
             and '.ssh' not in {part.casefold() for part in path.parts}
             and not path.name.casefold().endswith(('.sqlite', '.sqlite3', '.db')),
             'Engines, credentials and queue databases cannot be fetched')
    return value


def normalize_fetch_spec(spec):
    fields = {'schema', 'storage', 'purpose', 'capture', 'large_files', 'files'}
    _require(isinstance(spec, dict) and set(spec) == fields and type(spec['schema']) is int
             and spec['schema'] == 1, 'Fetch requires the exact schema-1 selection document')
    _require(isinstance(spec['storage'], str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', spec['storage']),
             'Select an enrolled export storage alias')
    _require(isinstance(spec['purpose'], str) and spec['purpose'] in {'summaries', 'analysis', 'archive', 'restart'},
             'Choose summaries, analysis, archive or restart')
    _require(isinstance(spec['capture'], str) and spec['capture'] in {'observation', 'stable'},
             'Capture must explicitly be observation or stable')
    _require(type(spec['large_files']) is bool, 'Large-file transfer requires an explicit boolean opt-in')
    _require(spec['purpose'] not in {'archive', 'restart'} or spec['capture'] == 'stable',
             'Archive/restart capture requires a terminal stable source')
    _require(not spec['large_files'] or spec['purpose'] in {'archive', 'restart'},
             'Large-file opt-in is limited to explicit archive or restart sets')
    files = spec['files']
    _require(isinstance(files, list) and 1 <= len(files) <= MAX_FILES,
             'Explicitly select 1..128 declared files; recursive fetching is unsupported')
    selected, destinations = set(), set()
    for item in files:
        _require(isinstance(item, dict) and set(item) == {'location_id', 'relative', 'path'},
                 'Each selection requires location_id, relative and replica path')
        _require(isinstance(item['location_id'], str) and re.fullmatch(r'[0-9a-f]{64}', item['location_id']),
                 'Use a current declared location ID from Files')
        relative = _relative(item['relative'], empty=True)
        destination = _relative(item['path'])
        folded = destination.casefold()
        _require(folded != _MANIFEST_NAME and not folded.startswith(_MANIFEST_NAME + '/')
                 and not any(folded == old or folded.startswith(old + '/') or old.startswith(folded + '/')
                             for old in destinations), 'Replica paths collide or use the reserved fetch manifest name')
        destinations.add(folded)
        key = item['location_id'], relative
        _require(key not in selected, 'The same declared source was selected more than once')
        selected.add(key)
        if PurePosixPath(relative or destination).name.casefold().startswith(('wavecar', 'chgcar')):
            _require(spec['purpose'] == 'restart' and spec['large_files'] and spec['capture'] == 'stable',
                     'Restart files require explicit restart purpose, large_files opt-in and stable capture')
    result = {**spec, 'files': sorted((dict(item) for item in files), key=lambda item: item['path'])}
    from .protocol import encode
    _require(len(encode(result)) <= 128 * 1024, 'Fetch selection exceeds 128 KiB')
    return result


def _limits(spec):
    return (LARGE_FILE_BYTES, LARGE_TOTAL_BYTES) if spec['large_files'] else (NORMAL_FILE_BYTES, NORMAL_TOTAL_BYTES)


def _space(root, count):
    check_storage_capacity(root, minimum_free_gb=(count + _MARGIN) / BYTES_PER_GIB)


def _records(backend, *, create=False):
    root = _private(Path(backend.config_root), directory=True) / 'fetches'
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    return _private(root, directory=True)


def _exports(backend, storage, *, create=False):
    namespace = _layout(backend.config, storage, create=create)
    _require(namespace is not None, 'No export namespace exists', 'FETCH_NOT_FOUND')
    exports = namespace / 'exports'
    identity = backend.config['storages'][storage]['storage_id']
    record = _records(backend, create=create) / ('storage-' + identity + '.json')
    if not record.exists() and create:
        # Existing unregistered directories are never silently adopted.
        exports.mkdir(mode=0o700)
        _write_json(record, {'schema': 1, 'storage_id': identity, 'path': str(exports),
                             'identity': _directory_id(exports)})
    owner = _read_json(record)
    _require(owner == {'schema': 1, 'storage_id': identity, 'path': str(exports),
                       'identity': _directory_id(_private(exports, directory=True))},
             'Export directory identity changed', 'STORAGE_CHANGED')
    return namespace, exports


def _load(backend, fetch_id):
    record = _read_json(_records(backend) / (identifier(fetch_id, 'fetch ID') + '.json'))
    fields = {'schema', 'fetch_id', 'target', 'spec', 'spec_digest', 'status', 'export_path',
              'export_identity', 'manifest', 'manifest_digest'}
    _require(set(record) == fields and type(record['schema']) is int and record['schema'] == 1
             and record['fetch_id'] == fetch_id, 'Malformed fetch ledger')
    target_identity(record['target'])
    spec = normalize_fetch_spec(record['spec'])
    _require(stable_hash({'target': record['target'], 'spec': spec}) == record['spec_digest'],
             'Fetch record contradicts its original selection')
    _, exports = _exports(backend, spec['storage'])
    _require(record['export_path'] == str(exports / fetch_id)
             and _directory_id(_private(Path(record['export_path']), directory=True)) == record['export_identity'],
             'Captured export directory was replaced', 'STORAGE_CHANGED')
    return record


def _receipt(record):
    return {key: record[key] for key in ('schema', 'fetch_id', 'target', 'status',
                                        'export_path', 'manifest', 'manifest_digest')} | {'storage': record['spec']['storage']}


def _sources(backend, job, spec):
    found = discover(backend.store, job['id'], expected_attempt_id=job['attempt_id'])
    locations = {item.key: item for item in found.locations}
    selected, total = [], 0
    maximum, total_maximum = _limits(spec)
    for item in spec['files']:
        location = locations.get(item['location_id'])
        _require(location is not None and location.accessibility == 'AVAILABLE' and not location.ambiguity,
                 'Selected declared location is missing, changed or ambiguous', 'LOCATION_UNAVAILABLE')
        _require(location.path and location.declared_path == location.path,
                 'Fetch cannot follow a symlink or substituted declared location')
        base = _direct(location.path, directory=location.kind == 'directory')
        if location.kind == 'file':
            _require(item['relative'] == '', 'A declared file requires an empty relative selector')
            path = base
        else:
            _require(item['relative'] != '', 'Select a file below the declared directory; recursive fetch is unsupported')
            path = _direct(base / item['relative'])
            _require(path.is_relative_to(base), 'Selected file escaped the declared directory')
        _relative(path.name)
        if path.name.casefold().startswith(('wavecar', 'chgcar')):
            _require(spec['purpose'] == 'restart' and spec['large_files'] and spec['capture'] == 'stable',
                     'Restart artifacts require stable explicit large-file opt-in')
        if path.is_relative_to(backend.store.root):
            _require(location.role == 'operational-logs' and location.kind == 'file',
                     'Only declared operational logs may be fetched from private Jobs state')
        _require(not path.is_relative_to(Path(backend.config_root)), 'Private node configuration cannot be fetched')
        for storage in backend.config['storages'].values():
            if path.is_relative_to(Path(storage['root'])):
                observed = check_storage(storage)
                _require(observed['ready'], 'Source storage is unavailable: ' + str(observed.get('error')), 'STORAGE_UNAVAILABLE')
        info = path.stat()
        _require(info.st_size <= maximum, f'Selected file exceeds the {maximum // 1024**2} MiB bound', 'FETCH_LIMIT')
        total += info.st_size
        _require(total <= total_maximum, f'Selected set exceeds the {total_maximum // 1024**2} MiB bound', 'FETCH_LIMIT')
        selected.append((item, location, path, _identity(info)))
    return selected, total


def _capture(path, destination, before, *, stable, maximum):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        _require(stat.S_ISREG(opened.st_mode) and _identity(opened) == before,
                 'Declared source changed before capture', 'SOURCE_CHANGED')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as output:
            # Observation is a bounded prefix of the opened version, even if a
            # running writer keeps appending. It is never a coherent archive.
            while count < before[2]:
                data = source.read(min(1024 * 1024, before[2] - count))
                if not data:
                    break
                count += len(data)
                _require(count <= maximum, 'Source exceeds the reviewed capture bound', 'FETCH_LIMIT')
                digest.update(data)
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        after = _identity(os.fstat(source.fileno()))
    try:
        current = _identity(_direct(path).stat())
    except (OSError, NodeError):
        current = None
    if stable:
        _require(before == after == current and count == before[2],
                 'Stable source changed during capture; request a new reviewed capture', 'SOURCE_CHANGED')
    return {'bytes': count, 'sha256': digest.hexdigest(), 'source_before': before,
            'source_after': after, 'path_after': current, 'changed': before != after or after != current or count != before[2]}


def prepare_fetch(backend, job, spec, request_id, *, _fault=None):
    """Capture explicitly selected declared files on their owning execution node."""
    spec = normalize_fetch_spec(spec)
    fetch_id = identifier(request_id, 'fetch ID')
    target = target_identity(backend.target(job))
    digest = stable_hash({'target': target, 'spec': spec})
    _require(spec['storage'] in backend.config['storages'], 'Export storage is not enrolled')
    namespace, exports = _exports(backend, spec['storage'], create=True)
    with _lock(namespace, fetch_id):
        record_path = _records(backend) / (fetch_id + '.json')
        if record_path.exists():
            record = _load(backend, fetch_id)
            _require(record['spec_digest'] == digest, 'Fetch ID already belongs to a different selection', 'REQUEST_CONFLICT')
            _require(record['status'] == 'CAPTURED', 'Interrupted capture retained; inspect it and use a new fetch ID', 'FETCH_INCOMPLETE')
            _verify_export(record)
            return _receipt(record)
        stable = spec['capture'] == 'stable'
        _require(not stable or job['status'] in TERMINAL,
                 'Stable archive/restart capture requires a terminal job; use an incomplete observation for active work')
        selected, total = _sources(backend, job, spec)
        _space(exports, total)
        export = exports / fetch_id
        export.mkdir(mode=0o700)
        (export / 'files').mkdir(mode=0o700)
        record = {'schema': 1, 'fetch_id': fetch_id, 'target': target, 'spec': spec, 'spec_digest': digest,
                  'status': 'CAPTURING', 'export_path': str(export), 'export_identity': _directory_id(export),
                  'manifest': None, 'manifest_digest': None}
        _write_json(record_path, record)
        files = []
        for index, (item, location, path, before) in enumerate(selected):
            observed = _capture(path, export / 'files' / f'{index:04d}.bin', before,
                                stable=stable, maximum=_limits(spec)[0])
            files.append({'path': item['path'], 'bytes': observed.pop('bytes'), 'sha256': observed.pop('sha256'),
                          'source': {'location_id': location.key, 'role': location.role,
                                     'node_path': str(path), 'relative': item['relative'], **observed}})
        if _fault:
            _fault('after_capture')
        latest = backend._job(target)
        if stable:
            _require(latest['status'] == job['status'] and latest['status'] in TERMINAL,
                     'Job lifecycle changed during stable capture', 'SOURCE_CHANGED')
            for _, _, path, before in selected:
                _require(_identity(_direct(path).stat()) == before,
                         'A source changed before the complete set was captured', 'SOURCE_CHANGED')
        _exports(backend, spec['storage'])
        manifest = {'schema': 1, 'fetch_id': fetch_id, 'target': target, 'storage_id': backend.config['storages'][spec['storage']]['storage_id'],
                    'purpose': spec['purpose'], 'capture': spec['capture'], 'large_files': spec['large_files'],
                    'job_status': job['status'], 'qualification': 'STABLE_REPLICA' if stable else 'INCOMPLETE_OBSERVATIONAL',
                    'scientific_validation': 'not-assessed', 'original_location_authority': False,
                    'files': files}
        record.update(status='CAPTURED', manifest=manifest, manifest_digest=stable_hash(manifest))
        _sync_directory(export / 'files')
        _require(len(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True).encode()) <= 192 * 1024 - 1,
                 'Captured source evidence exceeds 192 KiB; narrow the explicit file set', 'FETCH_LIMIT')
        _write_json(record_path, record)
        if _fault:
            _fault('after_record')
        return _receipt(record)


def _validated_receipt(receipt):
    fields = {'schema', 'fetch_id', 'target', 'storage', 'status', 'export_path', 'manifest', 'manifest_digest'}
    _require(isinstance(receipt, dict) and set(receipt) == fields and type(receipt['schema']) is int
             and receipt['schema'] == 1 and receipt['status'] == 'CAPTURED', 'Malformed captured-export receipt')
    fetch_id = identifier(receipt['fetch_id'], 'fetch ID')
    target_identity(receipt['target'])
    manifest = receipt['manifest']
    wanted = {'schema', 'fetch_id', 'target', 'storage_id', 'purpose', 'capture', 'large_files', 'job_status',
              'qualification', 'scientific_validation', 'original_location_authority', 'files'}
    _require(isinstance(manifest, dict) and set(manifest) == wanted and type(manifest['schema']) is int
             and manifest['schema'] == 1 and manifest['fetch_id'] == fetch_id and manifest['target'] == receipt['target']
             and stable_hash(manifest) == receipt['manifest_digest'], 'Export manifest contradicts its target or digest')
    identifier(manifest['storage_id'], 'storage ID')
    _require(isinstance(manifest['job_status'], str) and manifest['scientific_validation'] == 'not-assessed' and manifest['original_location_authority'] is False,
             'Fetched replicas cannot assert scientific acceptance or original-location authority')
    files = manifest['files']
    _require(isinstance(files, list), 'Export files must be an explicit list')
    selections, total = [], 0
    for item in files:
        _require(isinstance(item, dict) and set(item) == {'path', 'bytes', 'sha256', 'source'}, 'Malformed exported file')
        source = item['source']
        _require(isinstance(source, dict) and set(source) == {'location_id', 'role', 'node_path', 'relative', 'source_before', 'source_after', 'path_after', 'changed'},
                 'Malformed original-source evidence')
        _require(isinstance(source['role'], str) and isinstance(source['node_path'], str)
                 and len(source['node_path']) <= 4096 and PurePosixPath(source['node_path']).is_absolute()
                 and all(ord(c) >= 32 and ord(c) != 127 for c in source['node_path'])
                 and type(source['changed']) is bool,
                 'Malformed source location identity')
        for key in ('source_before', 'source_after', 'path_after'):
            value = source[key]
            _require((key == 'path_after' and value is None) or
                     (isinstance(value, list) and len(value) == 5 and all(type(v) is int and v >= 0 for v in value)),
                     'Malformed source stat epoch')
        _require(type(item['bytes']) is int and item['bytes'] >= 0 and isinstance(item['sha256'], str)
                 and re.fullmatch(r'[0-9a-f]{64}', item['sha256']), 'Malformed captured byte identity')
        selections.append({'location_id': source['location_id'], 'relative': source['relative'], 'path': item['path']})
        total += item['bytes']
    spec = normalize_fetch_spec({'schema': 1, 'storage': receipt['storage'], 'purpose': manifest['purpose'],
        'capture': manifest['capture'], 'large_files': manifest['large_files'], 'files': selections})
    maximum, total_maximum = _limits(spec)
    _require(total <= total_maximum and all(item['bytes'] <= maximum for item in files), 'Captured payload exceeds its bounds', 'FETCH_LIMIT')
    qualification = 'STABLE_REPLICA' if spec['capture'] == 'stable' else 'INCOMPLETE_OBSERVATIONAL'
    _require(manifest['qualification'] == qualification, 'Capture qualification contradicts the requested mode')
    if spec['capture'] == 'stable':
        _require(manifest['job_status'] in TERMINAL and all(not item['source']['changed'] and
                 item['source']['source_before'] == item['source']['source_after'] == item['source']['path_after']
                 and item['bytes'] == item['source']['source_before'][2] for item in files),
                 'Stable replicas require terminal, unchanged whole-file source evidence')
    export = receipt['export_path']
    _require(isinstance(export, str) and len(export) <= 4096 and all(ord(c) >= 32 and ord(c) != 127 for c in export)
             and '\\' not in export, 'Malformed remote export path')
    path = PurePosixPath(export)
    _require(path.is_absolute() and str(path) == export and '..' not in path.parts
             and path.name == fetch_id and path.parent.name == 'exports'
             and path.parent.parent.name == 'cmw-' + receipt['target']['node_id'],
             'Export path is outside the selected node namespace')
    return manifest



def _verify_bytes(path, expected):
    path = _direct(path)
    before = _identity(path.stat())
    _require(before[2] == expected['bytes'], 'Captured file size differs from its manifest', 'DIGEST_MISMATCH')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as source:
        _require(_identity(os.fstat(source.fileno())) == before, 'Captured file changed before verification', 'SOURCE_CHANGED')
        while True:
            data = source.read(min(1024 * 1024, expected['bytes'] + 1 - count))
            if not data:
                break
            count += len(data)
            digest.update(data)
            _require(count <= expected['bytes'], 'Captured file grew during verification', 'DIGEST_MISMATCH')
        _require(_identity(os.fstat(source.fileno())) == before == _identity(path.stat()),
                 'Captured file changed during verification', 'SOURCE_CHANGED')
    _require(count == expected['bytes'] and digest.hexdigest() == expected['sha256'],
             'Captured bytes differ from the manifest', 'DIGEST_MISMATCH')


def _verify_export(record):
    _require(record['status'] == 'CAPTURED', 'Export capture is incomplete', 'FETCH_INCOMPLETE')
    manifest = _validated_receipt(_receipt(record))
    root = _private(Path(record['export_path']), directory=True)
    _require(_directory_id(root) == record['export_identity'], 'Export directory was replaced', 'STORAGE_CHANGED')
    files = _private(root / 'files', directory=True)
    _require({p.name for p in root.iterdir()} == {'files'} and
             {p.name for p in files.iterdir()} == {f'{i:04d}.bin' for i in range(len(manifest['files']))},
             'Export contains missing or unexpected files', 'DIGEST_MISMATCH')
    for index, item in enumerate(manifest['files']):
        _verify_bytes(_private(files / f'{index:04d}.bin'), item)


def complete_fetch(backend, job, fetch_id, manifest_digest):
    """Acknowledge a verified capture; retained origin records remain on this node."""
    record = _load(backend, fetch_id)
    _require(record['target'] == backend.target(job) and record['manifest_digest'] == manifest_digest,
             'Fetch acknowledgment belongs to another target or manifest', 'WRONG_TARGET')
    _verify_export(record)
    return {'status': 'ACKNOWLEDGED', 'fetch_id': fetch_id, 'manifest_digest': manifest_digest,
            'qualification': record['manifest']['qualification'], 'source_retained': True}


def _destination(receipt, destination):
    manifest = _validated_receipt(receipt)
    destination = Path(destination)
    _require(destination.is_absolute() and destination == destination.resolve() and not os.path.lexists(destination),
             'Fetch requires a new canonical absolute destination; existing work is never overwritten')
    parent = _direct(destination.parent, directory=True)
    _space(parent, sum(item['bytes'] for item in manifest['files']))
    incoming = parent / ('.cmw-fetch-' + uuid4().hex)
    incoming.mkdir(mode=0o700)
    (incoming / 'data').mkdir(mode=0o700)
    return manifest, destination, incoming, _directory_id(parent)


def _replica_manifest(receipt, manifest):
    return {'schema': 1, 'kind': 'cmw-fetched-replica',
        'fetch_id': receipt['fetch_id'], 'origin': receipt['target'], 'manifest_digest': receipt['manifest_digest'],
        'manifest': manifest, 'original_location_authority': False,
        'relocated_finalization': 'unsupported; verify native origin on the execution node'}


def verify_replica(receipt, destination, publication):
    """Recover only our recorded directory promotion, never an identical copy."""
    manifest = _validated_receipt(receipt)
    destination = _private(Path(destination), directory=True)
    _require(isinstance(publication, dict) and set(publication) ==
             {'destination', 'directory_identity', 'parent_identity', 'manifest_digest'},
             'Missing owned replica publication identity', 'UNVERIFIED_REPLICA')
    for key in ('directory_identity', 'parent_identity'):
        value = publication[key]
        _require(isinstance(value, list) and len(value) == 2 and all(type(v) is int and v >= 0 for v in value),
                 'Malformed replica publication identity', 'UNVERIFIED_REPLICA')
    _require(publication['destination'] == str(destination) and publication['manifest_digest'] == receipt['manifest_digest']
             and _directory_id(destination) == publication['directory_identity']
             and _directory_id(destination.parent) == publication['parent_identity'],
             'Existing destination is not the recorded owned replica', 'UNVERIFIED_REPLICA')
    expected_files = {_MANIFEST_NAME, *(item['path'] for item in manifest['files'])}
    expected_directories = {str(parent) for name in expected_files for parent in PurePosixPath(name).parents
                            if str(parent) != '.'}
    files, directories = set(), set()
    for path in destination.rglob('*'):
        _private(path, directory=path.is_dir())
        (directories if path.is_dir() else files).add(str(path.relative_to(destination)))
    _require(files == expected_files and directories == expected_directories,
             'Existing replica has missing or unexpected entries', 'UNVERIFIED_REPLICA')
    _require(_read_json(destination / _MANIFEST_NAME) == _replica_manifest(receipt, manifest),
             'Existing replica manifest differs from the recorded capture', 'UNVERIFIED_REPLICA')
    for item in manifest['files']:
        _verify_bytes(destination / item['path'], item)
    return {'status': 'FETCHED', 'destination': str(destination), 'manifest_digest': receipt['manifest_digest'],
            'qualification': manifest['qualification']}


def _publish_download(receipt, manifest, destination, incoming, parent_identity, *, _before_publish=None):
    data = _private(incoming / 'data', directory=True)
    for index, item in enumerate(manifest['files']):
        flat = _private(data / f'{index:04d}.bin')
        _verify_bytes(flat, item)
    candidate = incoming / 'replica'
    candidate.mkdir(mode=0o700)
    for index, item in enumerate(manifest['files']):
        path = candidate / item['path']
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.rename(data / f'{index:04d}.bin', path)
    _write_json(candidate / _MANIFEST_NAME, _replica_manifest(receipt, manifest))
    for directory in sorted((p for p in candidate.rglob('*') if p.is_dir()), reverse=True):
        _sync_directory(directory)
    _sync_directory(candidate)
    if _before_publish is not None:
        _before_publish({'destination': str(destination), 'directory_identity': _directory_id(candidate),
                         'parent_identity': parent_identity, 'manifest_digest': receipt['manifest_digest']})
    _require(_directory_id(destination.parent) == parent_identity, 'Local destination parent changed', 'STORAGE_CHANGED')
    _rename_exclusive(candidate, destination)
    _sync_directory(destination.parent)
    data.rmdir()
    incoming.rmdir()
    return {'status': 'FETCHED', 'destination': str(destination), 'manifest_digest': receipt['manifest_digest'],
            'qualification': manifest['qualification']}


def download_fetch(connection, receipt, destination, *, _argv=None, _host_key=None, timeout=120, _before_publish=None):
    """Fetch only captured flat exports through an independent pinned SFTP channel."""
    manifest, destination, incoming, parent_identity = _destination(receipt, destination)
    commands = [f'get {sftp_quote(str(PurePosixPath(receipt["export_path"]) / "files" / f"{i:04d}.bin"))} {i:04d}.bin'
                for i in range(len(manifest['files']))]
    argv = sftp_command(connection) if _argv is None else _argv
    key = connection['ssh_host_key'] if _argv is None else _host_key
    run_sftp(argv, ('\n'.join(commands) + '\nbye\n').encode(), cwd=incoming / 'data', host_key=key, timeout=timeout)
    return _publish_download(receipt, manifest, destination, incoming, parent_identity, _before_publish=_before_publish)


def download_local_fetch(receipt, destination, *, _before_publish=None):
    """Explicit local-node copy; remote-looking paths never implicitly use it."""
    manifest, destination, incoming, parent_identity = _destination(receipt, destination)
    for index, item in enumerate(manifest['files']):
        source = _private(Path(receipt['export_path']) / 'files' / f'{index:04d}.bin')
        observed = _capture(source, incoming / 'data' / f'{index:04d}.bin', _identity(source.stat()),
                            stable=True, maximum=item['bytes'])
        _require(observed['sha256'] == item['sha256'] and observed['bytes'] == item['bytes'],
                 'Local export differs from the capture manifest', 'DIGEST_MISMATCH')
    return _publish_download(receipt, manifest, destination, incoming, parent_identity, _before_publish=_before_publish)
