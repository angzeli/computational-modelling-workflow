"""Explicit node enrollment and read-only host/runtime/queue readiness."""
from __future__ import annotations

from importlib import metadata
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from uuid import UUID, uuid4

from cmw.core.provenance import canonical_json_bytes, stable_hash
from .storage import identify_storage, check_storage, validate_storage


MAX_CONFIG_BYTES = 1024 * 1024
_ALIAS = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,47}\Z')
_CONFIG_FIELDS = {'schema', 'node_id', 'host_id', 'host', 'runtime', 'queues', 'storages', 'profiles'}
_PROFILE_FIELDS = {'engine', 'runner', 'max_cpus', 'max_ranks', 'dependencies'}


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ValueError(f'Malformed {label}: unexpected or missing fields')
    return value


def _uuid(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError(f'{label} must be a lowercase 32-hex UUID')
    return value


def _aliases(value, label):
    if not isinstance(value, dict) or not value or len(value) > 32 or any(
            not isinstance(key, str) or _ALIAS.fullmatch(key) is None for key in value):
        raise ValueError(f'{label} must contain 1..32 named entries')
    if len({key.casefold() for key in value}) != len(value):
        raise ValueError(f'{label} contains ambiguous aliases')
    return value


def _absolute(value, label):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        raise ValueError(f'{label} must be an absolute path')
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError(f'{label} must be canonical without symlinks')
    return path


def _host(value):
    _object(value, {'platform', 'hardware_id'}, {'platform', 'hardware_id'}, 'host evidence')
    if value['platform'] not in {'darwin', 'linux'} or not isinstance(value['hardware_id'], str):
        raise ValueError('Unsupported host identity evidence')
    hardware = value['hardware_id']
    if value['platform'] == 'darwin':
        if str(UUID(hardware)) != hardware or UUID(hardware).int == 0:
            raise ValueError('Platform hardware identity must be a canonical nonzero UUID')
    elif not re.fullmatch(r'[0-9a-f]{32}', hardware) or hardware == '0' * 32:
        raise ValueError('Linux machine-id is unavailable or invalid')
    return value


def host_evidence():
    """Use persistent local platform evidence, never a hostname or network probe."""
    if sys.platform == 'darwin':
        result = subprocess.run(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                                check=True, capture_output=True, timeout=5)
        if len(result.stdout) > 128 * 1024:
            raise ValueError('Hardware evidence exceeds 128 KiB')
        matches = re.findall(rb'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', result.stdout)
        if len(matches) != 1:
            raise ValueError('One platform hardware UUID is required')
        return _host({'platform': 'darwin', 'hardware_id': matches[0].decode().lower()})
    if sys.platform.startswith('linux'):
        with Path('/etc/machine-id').open('rb') as handle:
            raw = handle.read(129)
        return _host({'platform': 'linux', 'hardware_id': raw.decode('ascii').strip()})
    raise ValueError('Node enrollment supports macOS and Linux only')


@lru_cache(maxsize=2)
def _package_digest(root, epoch):
    """Reuse only a matching complete package stat epoch; never cache file data."""
    digest = hashlib.sha256()
    for name, expected in epoch:
        path = root / name
        before = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if identity != expected or not stat.S_ISREG(before.st_mode):
            raise ValueError('CMW runtime changed during fingerprinting')
        data = path.read_bytes()
        after = path.lstat()
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
            raise ValueError('CMW runtime changed during fingerprinting')
        encoded = name.encode('utf-8')
        digest.update(len(encoded).to_bytes(4, 'big') + encoded + len(data).to_bytes(8, 'big') + data)
    return digest.hexdigest()


def runtime_identity():
    """Fingerprint actual package bytes, interpreter and installed dependencies."""
    import cmw
    root = Path(cmw.__file__).resolve().parent
    total = 0
    epoch = []
    paths = sorted(path for path in root.rglob('*') if path.is_file()
                   and '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.pyo'}
                   and path.name != '.DS_Store')
    if not paths or len(paths) > 4096:
        raise ValueError('CMW package file inventory is unavailable or oversized')
    for path in paths:
        observed = path.lstat()
        total += observed.st_size
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > 16 * 1024**2 or total > 128 * 1024**2:
            raise ValueError('CMW package contains unsupported or oversized files')
        identity = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)
        epoch.append((str(path.relative_to(root)), identity))
    package_digest = _package_digest(root, tuple(epoch))
    dependencies = {}
    for name in ('ase', 'gemmi', 'numpy', 'PyYAML', 'psutil', 'textual', 'rich'):
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    try:
        version = metadata.version('computational-modelling-workflow')
    except metadata.PackageNotFoundError:
        version = getattr(cmw, '__version__', 'uninstalled')
    record = {'algorithm': 'cmw-runtime-sha256-v1', 'package_sha256': package_digest,
              'version': version, 'python': sys.version, 'dependencies': dependencies,
              'package_origin': str(root), 'executable': sys.executable}
    # Locators are disclosed separately; identical installed bytes do not acquire
    # another node's filesystem/process authority.
    record['fingerprint'] = stable_hash({key: value for key, value in record.items()
                                      if key not in {'package_origin', 'executable'}})
    return record


def _file_identity(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or '\0' in value:
        raise ValueError('Profile dependency paths must be absolute')
    path = Path(value)
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024**2:
        raise ValueError('Profile dependency must be a bounded regular file')
    digest = hashlib.sha256()
    with resolved.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    after = resolved.stat()
    identity = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns]
    if identity != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns] or path.resolve(strict=True) != resolved:
        raise ValueError('Profile dependency changed during fingerprinting')
    return {'path': str(path), 'resolved_path': str(resolved), 'identity': identity,
            'bytes': before.st_size, 'sha256': digest.hexdigest()}


def _profile(spec, runtime):
    _object(spec, _PROFILE_FIELDS, {'engine', 'runner', 'max_cpus'}, 'runner profile')
    if spec['engine'] not in {'Command', 'VASP'}:
        raise ValueError('Profile engine must be Command or VASP')
    cpus = spec['max_cpus']
    if type(cpus) is not int or not 1 <= cpus <= 256:
        raise ValueError('Profile max_cpus must be an integer in 1..256')
    ranks = spec.get('max_ranks', 1 if spec['engine'] == 'Command' else min(8, cpus))
    if type(ranks) is not int or not 1 <= ranks <= (8 if spec['engine'] == 'VASP' else cpus) or ranks > cpus:
        raise ValueError('VASP profiles retain the supported 1..8 rank limit')
    runner = _file_identity(spec['runner'])
    if not os.access(spec['runner'], os.X_OK):
        raise ValueError('Configured runner is not executable')
    dependencies = spec.get('dependencies', [])
    if (not isinstance(dependencies, list) or len(dependencies) > 32
            or any(not isinstance(item, str) for item in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise ValueError('Profile dependencies must be an explicit unique list of at most 32 files')
    if spec['engine'] == 'VASP':
        if Path(spec['runner']).name != 'run-vasp.sh':
            raise ValueError('VASP profile requires the recognized run-vasp.sh entry point')
        required = [str(Path(spec['runner']).parent / name)
                    for name in ('run_vasp.py', 'environment.sh', 'launch.py', 'observables.py')]
        dependencies = sorted(set(dependencies + required))
    return {'engine': spec['engine'], 'runner': spec['runner'], 'max_cpus': cpus, 'max_ranks': ranks,
            'runner_identity': runner, 'dependencies': [_file_identity(path) for path in dependencies],
            'runtime_fingerprint': runtime['fingerprint'],
            'qualification': {'implementation': 'configured', 'synthetic_tested': False,
                              'real_engine': False, 'suspend_continue': False}}


def _private_root(config_root, *, create=False):
    path = _absolute(str(config_root), 'Node configuration root')
    if create:
        path.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Node configuration root must be owner-only (0700)')
    return path


def _decode(raw):
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Node configuration exceeds 1 MiB')
    def pairs(items):
        value = {}
        for key, entry in items:
            if key in value:
                raise ValueError('Duplicate node configuration key')
            value[key] = entry
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))


def _validate_file(value):
    fields = {'path', 'resolved_path', 'identity', 'bytes', 'sha256'}
    _object(value, fields, fields, 'profile file identity')
    for key in ('path', 'resolved_path'):
        if (not isinstance(value[key], str) or not Path(value[key]).is_absolute()
                or len(value[key]) > 4096 or '\0' in value[key]):
            raise ValueError('Profile file paths must be bounded absolute paths')
    if (type(value['bytes']) is not int or not 0 <= value['bytes'] <= 64 * 1024**2
            or not isinstance(value['sha256'], str)
            or re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is None):
        raise ValueError('Malformed pinned profile digest or size')
    identity = value['identity']
    if (not isinstance(identity, list) or len(identity) != 4
            or any(type(item) is not int or item < 0 for item in identity)
            or identity[2] != value['bytes']):
        raise ValueError('Malformed pinned profile file identity')


def _validate_runtime(value):
    fields = {'algorithm', 'package_sha256', 'version', 'python', 'dependencies',
              'package_origin', 'executable', 'fingerprint'}
    _object(value, fields, fields, 'runtime identity')
    if value['algorithm'] != 'cmw-runtime-sha256-v1':
        raise ValueError('Unsupported runtime fingerprint algorithm')
    for key in ('fingerprint', 'package_sha256'):
        if not isinstance(value[key], str) or re.fullmatch(r'[0-9a-f]{64}', value[key]) is None:
            raise ValueError('Node runtime fingerprint is invalid')
    for key in ('version', 'python', 'package_origin', 'executable'):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096 or '\0' in value[key]:
            raise ValueError('Malformed runtime identity field')
    dependencies = value['dependencies']
    if (not isinstance(dependencies, dict) or len(dependencies) > 32
            or any(not isinstance(key, str) or not key or len(key) > 128
                   or not (item is None or isinstance(item, str) and len(item) <= 128)
                   for key, item in dependencies.items())):
        raise ValueError('Malformed dependency version evidence')


def _validate_config(value):
    _object(value, _CONFIG_FIELDS, _CONFIG_FIELDS, 'node configuration')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported node configuration schema')
    _uuid(value['node_id'], 'Node identity')
    host = _host(value['host'])
    if value['host_id'] != host['platform'] + ':' + host['hardware_id']:
        raise ValueError('Node host identity contradicts its platform evidence')
    _validate_runtime(value['runtime'])
    for alias, queue in _aliases(value['queues'], 'Queues').items():
        fields = {'schema', 'node_id', 'host_id', 'queue_id', 'state_root', 'state',
                  'root_identity', 'database_identity'}
        _object(queue, fields, fields, 'queue enrollment')
        if type(queue['schema']) is not int or queue['schema'] != 1:
            raise ValueError('Unsupported queue identity schema')
        for key in ('root_identity', 'database_identity'):
            if (not isinstance(queue[key], list) or len(queue[key]) != 2
                    or any(type(item) is not int or item < 0 for item in queue[key])):
                raise ValueError('Malformed enrolled queue filesystem identity')
        if queue.get('node_id') != value['node_id'] or queue.get('host_id') != value['host_id']:
            raise ValueError(f'Queue {alias} belongs to another node or host')
        _uuid(queue.get('queue_id'), 'Queue identity')
        if queue.get('state') != queue.get('state_root'):
            raise ValueError('Queue state root contradicts enrollment')
        _absolute(queue['state'], 'Queue state')
    for storage in _aliases(value['storages'], 'Storages').values():
        validate_storage(storage)
    for profile in _aliases(value['profiles'], 'Profiles').values():
        fields = {'engine', 'runner', 'max_cpus', 'max_ranks', 'runner_identity',
                  'dependencies', 'runtime_fingerprint', 'qualification'}
        _object(profile, fields, fields, 'pinned runner profile')
        if profile['runtime_fingerprint'] != value['runtime']['fingerprint']:
            raise ValueError('Profile runtime does not match the node pin')
        if (profile['engine'] not in {'Command', 'VASP'} or type(profile['max_cpus']) is not int
                or not 1 <= profile['max_cpus'] <= 256 or type(profile['max_ranks']) is not int
                or not 1 <= profile['max_ranks'] <= profile['max_cpus']
                or profile['engine'] == 'VASP' and profile['max_ranks'] > 8):
            raise ValueError('Unsupported pinned runner resources')
        _validate_file(profile['runner_identity'])
        if profile['runner'] != profile['runner_identity']['path']:
            raise ValueError('Pinned runner path contradicts its identity')
        if not isinstance(profile['dependencies'], list) or len(profile['dependencies']) > 36:
            raise ValueError('Malformed pinned dependency list')
        for dependency in profile['dependencies']:
            _validate_file(dependency)
        if profile['qualification'] != {'implementation': 'configured', 'synthetic_tested': False,
                                         'real_engine': False, 'suspend_continue': False}:
            raise ValueError('Configuration alone cannot establish scientific qualification')
    return value


def load_node(config_root):
    """Read only; a missing node or queue is never initialized or migrated."""
    root = _private_root(config_root)
    path = root / 'node.json'
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_CONFIG_BYTES):
        raise ValueError('Node configuration must be an owner-only regular file')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError('Node configuration changed while opening')
        raw = handle.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(handle.fileno())
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Node configuration changed while reading')
    return _validate_config(_decode(raw))


def enroll(config_root, spec, *, host_provider=None, runtime_provider=None,
           storage_provider=None, store_factory=None):
    """Explicitly create private node and queue identities; no execution occurs."""
    from cmw.jobs.store import Store
    _object(spec, {'node_id', 'queues', 'storages', 'profiles'}, {'queues', 'storages', 'profiles'}, 'node enrollment')
    node_id = _uuid(spec.get('node_id', uuid4().hex), 'Node identity')
    host = _host((host_evidence if host_provider is None else host_provider)())
    runtime = (runtime_identity if runtime_provider is None else runtime_provider)()
    _validate_runtime(runtime)
    host_id = host['platform'] + ':' + host['hardware_id']
    queues = _aliases(spec['queues'], 'Queues')
    states = [_absolute(state, 'Queue state') for state in queues.values()]
    if len(set(states)) != len(states):
        raise ValueError('Each enrolled queue alias must name a distinct state root')
    storages = {alias: (identify_storage(root) if storage_provider is None else storage_provider(root))
                for alias, root in _aliases(spec['storages'], 'Storages').items()}
    for value in storages.values():
        validate_storage(value)
    if len({value['root'] for value in storages.values()}) != len(storages):
        raise ValueError('Each storage alias must name a distinct root')
    profiles = {alias: _profile(profile, runtime)
                for alias, profile in _aliases(spec['profiles'], 'Profiles').items()}
    root = _absolute(str(config_root), 'Node configuration root')
    if root.exists():
        _private_root(root)
        if (root / 'node.json').exists():
            raise ValueError('Node is already enrolled; existing identity is never overwritten')
    else:
        _private_root(root, create=True)
    store_type = Store if store_factory is None else store_factory
    queue_records = {}
    for (alias, _), state in zip(queues.items(), states):
        record = store_type(state).enroll_queue(node_id, host_id)
        queue_records[alias] = {**record, 'state': str(state)}
    value = _validate_config({'schema': 1, 'node_id': node_id, 'host_id': host_id, 'host': host,
                              'runtime': runtime, 'queues': queue_records, 'storages': storages,
                              'profiles': profiles})
    content = canonical_json_bytes(value)
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError('Node configuration exceeds 1 MiB')
    descriptor = os.open(root / 'node.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(content + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return value


def check_node(config, *, host_provider=None, runtime_provider=None,
               storage_provider=None, store_factory=None):
    """Observe readiness; missing storage does not erase independent queue evidence."""
    from cmw.jobs.store import Store
    _validate_config(config)
    result = {'ready': False, 'identity_ready': True, 'runtime_ready': True,
              'storage_ready': True, 'profiles_ready': True, 'identity_errors': [],
              'runtime_errors': [], 'errors': [], 'host': None, 'runtime': None,
              'queues': {}, 'storages': {}, 'profiles': {}}
    try:
        result['host'] = _host((host_evidence if host_provider is None else host_provider)())
        if result['host'] != config['host']:
            raise ValueError('Node hardware identity changed or configuration was cloned')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result['identity_errors'].append(str(exc))
    try:
        result['runtime'] = (runtime_identity if runtime_provider is None else runtime_provider)()
        if result['runtime']['fingerprint'] != config['runtime']['fingerprint']:
            raise ValueError('Actual CMW runtime differs from the enrolled fingerprint')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['runtime_errors'].append(str(exc))
    store_type = Store if store_factory is None else store_factory
    for alias, expected in config['queues'].items():
        try:
            observed = store_type(expected['state']).queue_identity()
            if observed != {key: value for key, value in expected.items() if key != 'state'}:
                raise ValueError('Queue identity changed or enrollment is unavailable')
            result['queues'][alias] = observed
        except (OSError, ValueError, RuntimeError) as exc:
            result['identity_errors'].append(f'{alias}: {exc}')
    for alias, expected in config['storages'].items():
        observed = (check_storage(expected) if storage_provider is None else storage_provider(expected))
        result['storages'][alias] = observed
        if observed.get('ready') is not True:
            result['storage_ready'] = False
            result['errors'].append(f'{alias}: {observed.get("error", "Storage unavailable")}')
    for alias, profile in config['profiles'].items():
        try:
            for expected in [profile['runner_identity'], *profile['dependencies']]:
                if _file_identity(expected['path']) != expected:
                    raise ValueError('Pinned runner/dependency identity changed')
            if not os.access(profile['runner'], os.X_OK):
                raise ValueError('Pinned runner is no longer executable')
            result['profiles'][alias] = {'ready': True, 'error': None}
        except (OSError, ValueError, KeyError) as exc:
            result['profiles_ready'] = False
            result['profiles'][alias] = {'ready': False, 'error': str(exc)}
            result['errors'].append(f'{alias}: {exc}')
    result['identity_ready'] = not result['identity_errors']
    result['runtime_ready'] = not result['runtime_errors']
    result['errors'] += result['identity_errors'] + result['runtime_errors']
    result['ready'] = all(result[key] for key in ('identity_ready', 'runtime_ready', 'storage_ready', 'profiles_ready'))
    return result
