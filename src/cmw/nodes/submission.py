"""Portable specification -> verified destination-local held registration.

Transfer lineage is not execution provenance. The existing Store hook records
prepared POSCAR identity only after publication at its final local path.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat

from cmw.core.provenance import stable_hash
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from .identity import load_node, check_node
from .protocol import NodeError, identifier
from .storage import check_storage

_SPEC_FIELDS = {'schema', 'request_id', 'node_id', 'queue_id', 'storage', 'profile', 'engine',
                'name', 'resources', 'manifest', 'arguments', 'vasp', 'progress_sources',
                'intent', 'dependencies_complete', 'note'}
_REQUIRED = _SPEC_FIELDS - {'arguments', 'vasp', 'progress_sources', 'note'}


def _require(condition, message, code='INVALID_SUBMISSION'):
    if not condition:
        raise NodeError(code, message)


def _relative(value):
    _require(isinstance(value, str) and 0 < len(value) <= 1024 and '\0' not in value,
             'Path reference must be a bounded relative path')
    path = PurePosixPath(value)
    _require(not path.is_absolute() and str(path) == value and all(part not in {'.', '..'} for part in path.parts)
             and '\\' not in value, 'Path reference must be canonical and cannot escape its declared root')
    return value


def normalize_spec(value):
    from .transfer import normalize_manifest
    _require(isinstance(value, dict) and not set(value) - _SPEC_FIELDS and _REQUIRED <= set(value),
             'Unexpected or missing portable submission fields')
    _require(type(value['schema']) is int and value['schema'] == 1, 'Submission requires schema 1')
    for key in ('request_id', 'node_id', 'queue_id'):
        identifier(value[key], key)
    _require(value['intent'] == 'held', 'Remote submission only registers Hold; release and dispatch are separate explicit actions')
    _require(value['dependencies_complete'] is True,
             'Review and explicitly declare all supporting dependencies before submission')
    for key in ('storage', 'profile'):
        _require(isinstance(value[key], str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', value[key]),
                 f'{key} must name an enrolled destination profile')
    _require(isinstance(value['engine'], str) and value['engine'] in {'Command', 'VASP'}, 'MVP submission supports trusted Command or recognized managed VASP routes')
    _require(isinstance(value['name'], str) and 0 < len(value['name'].strip()) <= 256 and '\0' not in value['name'],
             'Name must be bounded literal text')
    resources = value['resources']
    _require(isinstance(resources, dict) and {'cpus', 'memory_gib'} <= set(resources)
             and not set(resources) - {'cpus', 'memory_gib', 'mpi_ranks', 'threads_per_rank'}, 'Explicit CPU and memory resources are required')
    for key, amount in resources.items():
        _require(type(amount) in (int, float) and math.isfinite(amount) and amount > 0,
                 f'{key} must be positive and finite')
        if key != 'memory_gib':
            _require(type(amount) is int, f'{key} must be an integer')
    resources = {'mpi_ranks': 1, 'threads_per_rank': 1, **resources}
    _require(resources['cpus'] >= resources['mpi_ranks'] * resources['threads_per_rank'],
             'CPU request understates the declared rank/thread demand')
    manifest = normalize_manifest(value['manifest'])
    _require(manifest['node_id'] == value['node_id'] and manifest['queue_id'] == value['queue_id'],
             'Bundle and submission target identities differ')
    arguments = value.get('arguments', [])
    _require(isinstance(arguments, list) and len(arguments) <= 128, 'Arguments must be a bounded literal list')
    for argument in arguments:
        if isinstance(argument, str):
            _require(len(argument) <= 8192 and '\0' not in argument and not argument.startswith('/'),
                     'Literal arguments cannot carry an undeclared absolute local path; use a typed dependency reference')
        else:
            _require(isinstance(argument, dict) and len(argument) == 1 and
                     next(iter(argument)) in {'bundle', 'run', 'dependency'}, 'Unsupported portable path reference')
            key, path = next(iter(argument.items()))
            if key == 'dependency':
                _require(type(path) is int and 0 <= path < 32, 'Dependency reference must index the pinned profile dependencies')
            else:
                _relative(path)
    sources = value.get('progress_sources', [])
    _require(isinstance(sources, list) and len(sources) <= 8, 'At most eight explicit progress-source roles are supported')
    for source in sources:
        _require(isinstance(source, dict) and {'role', 'path'} <= set(source)
                 and not set(source) - {'role', 'path', 'stage', 'optional'}, 'Malformed progress source')
        _require(isinstance(source['role'], str) and isinstance(source['path'], dict)
                 and len(source['path']) == 1 and next(iter(source['path'])) in {'bundle', 'run'},
                 'Progress source must use a destination-local bundle or run reference')
        _relative(next(iter(source['path'].values())))
        _require(type(source.get('optional', False)) is bool, 'Source optional flag must be boolean')
    if value['engine'] == 'VASP':
        _require(not arguments and isinstance(value.get('vasp'), dict), 'VASP uses the explicit managed runner settings')
        _require({item['path'] for item in manifest['files'] if item['path'].startswith('inputs/')} ==
                 {'inputs/INCAR', 'inputs/POSCAR', 'inputs/KPOINTS', 'inputs/POTCAR'},
                 'VASP requires exactly the existing four-file input bundle')
    else:
        _require('vasp' not in value, 'Command specification cannot carry VASP settings')
    result = {**value, 'resources': resources, 'manifest': manifest, 'arguments': arguments,
              'progress_sources': sources, 'note': value.get('note', '')}
    from cmw.jobs.store import validate_note
    result['note'] = validate_note(result['note'])
    _require(len(json.dumps(result).encode()) <= 128 * 1024, 'Submission exceeds 128 KiB')
    return result


def submission_digest(spec):
    normalized = normalize_spec(spec)
    return stable_hash({key: value for key, value in normalized.items() if key != 'request_id'})


def _paths(config, spec):
    storage = config['storages'][spec['storage']]
    namespace = Path(storage['root']) / ('cmw-' + config['node_id'])
    bundle = namespace / 'prepared' / spec['request_id']
    work = namespace / 'runs' / spec['request_id']
    return bundle, work, work / 'native'


def _resolve_argument(argument, bundle, work, profile, manifest):
    if isinstance(argument, str):
        return argument
    key, value = next(iter(argument.items()))
    if key == 'dependency':
        _require(value < len(profile['dependencies']), 'Required target-local dependency is not enrolled')
        return profile['dependencies'][value]['path']
    if key == 'bundle':
        _require(value in {item['path'] for item in manifest['files']}, 'Required supporting file is absent from the selected manifest')
        return str(bundle / value)
    return str(work / value)


def _argv(spec, profile, bundle, work, native):
    if spec['engine'] == 'Command':
        return [profile['runner'], *[_resolve_argument(item, bundle, work, profile, spec['manifest'])
                                     for item in spec['arguments']]]
    options = spec['vasp']
    keys = {'ncore', 'kpar', 'mpi_mode', 'timeout', 'stop_before'}
    _require(set(options) == keys, 'VASP requires explicit ncore, kpar, mpi_mode, timeout and stop_before')
    _require(isinstance(options['mpi_mode'], str) and options['mpi_mode'] in {'synthetic', 'native'}, 'Unsupported VASP MPI mode')
    for key in ('ncore', 'kpar'):
        _require(type(options[key]) is int and options[key] > 0, f'{key} must be a positive integer')
    for key in ('timeout', 'stop_before'):
        _require(type(options[key]) in (int, float) and math.isfinite(options[key]), f'{key} must be finite')
    argv = [profile['runner'], '--input', str(bundle / 'inputs'), '--output', str(native),
            '--binary', 'std', '--ranks', str(spec['resources']['mpi_ranks']),
            '--ncore', str(options['ncore']), '--kpar', str(options['kpar']),
            '--mpi-mode', options['mpi_mode'], '--restart', 'none',
            '--timeout', str(options['timeout']), '--stop-before', str(options['stop_before']),
            '--managed-foreground']
    from cmw.periodic.vasp.result_runner import validate_runner_argv
    validate_runner_argv(argv)
    from cmw.periodic.vasp.runner_compatibility import check_runner
    interface = check_runner(profile['runner'], argv[1:], allow_planned_input=True)
    _require(interface['exit_code'] == 0, 'Target runner interface is unqualified: ' + '; '.join(interface['missing_requirements']))
    return argv


def preview(config, queue, spec):
    spec = normalize_spec(spec)
    _require(spec['node_id'] == config['node_id'] and spec['queue_id'] == queue['queue_id'], 'Submission targets a different enrolled node or queue')
    _require(spec['storage'] in config['storages'] and spec['profile'] in config['profiles'], 'Required destination storage/runtime profile is not enrolled')
    readiness = check_node(config)
    _require(readiness['ready'], 'Destination preflight failed: ' + '; '.join(readiness['errors']), 'NOT_READY')
    profile = config['profiles'][spec['profile']]
    _require(profile['engine'] == spec['engine'], 'Submission engine differs from target runtime profile')
    _require(spec['resources']['cpus'] <= profile['max_cpus'] and spec['resources']['mpi_ranks'] <= profile['max_ranks'],
             'Requested resources exceed the pinned profile limits; VASP qualification remains at most eight ranks')
    bundle, work, native = _paths(config, spec)
    argv = _argv(spec, profile, bundle, work, native)
    return {'status': 'PREVIEW', 'intent': 'Hold', 'specification': spec, 'digest': submission_digest(spec),
            'argv': argv, 'cwd': str(work), 'input_bundle': str(bundle), 'native_output': str(native),
            'resources': spec['resources'], 'runtime_id': config['runtime']['fingerprint'],
            'qualification': profile['qualification'],
            'execution': 'Not authorized by submission; release and start/resume dispatch remain explicit',
            'queue_order': 'A held queue head blocks later entries under the existing order contract'}


def register_held(backend, spec, request_id):
    from .transfer import lookup_transfer
    spec = normalize_spec(spec)
    _require(request_id == spec['request_id'], 'Registration request ID differs from the persisted portable request')
    digest = submission_digest(spec)
    previous = backend.store.request_lookup(request_id)
    if previous is not None:
        _require(previous['operation'] == 'submit' and previous['digest'] == digest,
                 'Request ID already belongs to a different semantic request', 'REQUEST_CONFLICT')
        return {'status': 'REGISTERED', 'replayed': True, 'job': previous['result'], 'request_id': request_id}
    plan = preview(backend.config, backend.queue, spec)
    receipt = lookup_transfer(backend.config, spec['storage'], request_id)
    _require(receipt and receipt['status'] == 'PUBLISHED', 'Verified published bundle is required before registration')
    from .transfer import manifest_digest
    _require(receipt['manifest_digest'] == manifest_digest(spec['manifest']), 'Published bundle belongs to a different manifest')
    bundle, work, native = _paths(backend.config, spec)
    _require(Path(receipt['published_root']) == bundle, 'Transfer published path differs from the destination plan')
    if spec['engine'] == 'VASP':
        from cmw.periodic.vasp.inputs import check_inputs
        checked = check_inputs(bundle / 'inputs')
        _require(checked['status'] == 'valid', 'Published VASP inputs fail the existing four-file checker')
        _require(not native.exists() and not native.is_symlink(), 'VASP runner output must remain absent until its own staging')
    runs = work.parent
    runs.mkdir(mode=0o700, exist_ok=True)
    _require(runs.resolve() == runs and runs.stat().st_dev == bundle.stat().st_dev, 'Run directory must remain on the registered local volume')
    try:
        work.mkdir(mode=0o700)
    except FileExistsError:
        _require(work.is_dir() and work.resolve() == work and not any(work.iterdir()),
                 'Unregistered request working directory is not empty; inspect retained evidence')
    _require(stat.S_IMODE(work.stat().st_mode) & 0o077 == 0, 'Working directory must be private')
    profile = backend.config['profiles'][spec['profile']]
    sources = tuple(SourceDescriptor(source['role'], _resolve_argument(source['path'], bundle, work, profile, spec['manifest']),
                    stage=source.get('stage'), optional=source.get('optional', False)) for source in spec['progress_sources'])
    metadata = {'schema': 1, 'node_id': spec['node_id'], 'queue_id': spec['queue_id'],
                'request_id': request_id, 'digest': digest, 'config_root': str(backend.config_root),
                'storage_alias': spec['storage'], 'storage_id': backend.config['storages'][spec['storage']]['storage_id'],
                'profile': spec['profile'], 'transfer_request_id': request_id,
                'manifest_digest': receipt['manifest_digest'], 'published_root': str(bundle),
                'runtime_id': backend.config['runtime']['fingerprint'], 'target_profile_digest': stable_hash(profile),
                'queue_state': str(backend.store.root), 'working_directory': str(work),
                'working_identity': {'device': work.stat().st_dev, 'inode': work.stat().st_ino}}
    # Recheck storage after directory preparation and immediately before the sole
    # queue transaction. The worker repeats this at final admission.
    _require(check_storage(backend.config['storages'][spec['storage']])['ready'], 'Storage disappeared before registration')
    job = backend.store.add(argv=plan['argv'], cwd=work, name=spec['name'], engine=spec['engine'],
                            **spec['resources'], hold=True, note=spec['note'], progress_sources=sources,
                            remote_request={'request_id': request_id, 'digest': digest}, node_submission=metadata)
    return {'status': 'REGISTERED', 'replayed': False, 'job': job, 'request_id': request_id}


def launch_blocker(job):
    """Destination-local preflight for enrolled jobs; no alternate dispatcher."""
    try:
        from .transfer import lookup_transfer
        binding = job['node_submission']
        config = load_node(binding['config_root'])
        readiness = check_node(config)
        _require(readiness['ready'], 'Node readiness failed: ' + '; '.join(readiness['errors']))
        _require(config['node_id'] == binding['node_id'] and config['runtime']['fingerprint'] == binding['runtime_id'], 'Node/runtime identity changed')
        queues = [item for item in config['queues'].values() if item['queue_id'] == binding['queue_id']]
        _require(len(queues) == 1 and queues[0]['state'] == binding['queue_state'], 'Original destination queue enrollment changed')
        work = Path(binding['working_directory'])
        _require(job['cwd'] == str(work) and work.resolve(strict=True) == work and work.is_dir()
                 and {'device': work.stat().st_dev, 'inode': work.stat().st_ino} == binding['working_identity'],
                 'Destination working directory identity changed')
        storage = config['storages'][binding['storage_alias']]
        _require(storage['storage_id'] == binding['storage_id'], 'Storage enrollment changed')
        _require(stable_hash(config['profiles'][binding['profile']]) == binding['target_profile_digest'], 'Runtime profile changed')
        receipt = lookup_transfer(config, binding['storage_alias'], binding['transfer_request_id'])
        _require(receipt and receipt['status'] == 'PUBLISHED' and receipt['manifest_digest'] == binding['manifest_digest']
                 and receipt['published_root'] == binding['published_root'], 'Published input identity changed')
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return 'Node submission preflight: ' + str(exc)


def dispatch(backend, operation, params, *, request_id):
    from .transfer import begin_transfer, publish_transfer, lookup_transfer
    if operation == 'request':
        _require(set(params) == {'request_id'}, 'Request lookup needs one original request ID')
        identifier(params['request_id'], 'request ID')
        return {'request': backend.store.request_lookup(params['request_id'])}
    if operation in {'submit.preview', 'submit'}:
        _require(set(params) == {'spec'}, 'Submission requires one portable specification')
        return preview(backend.config, backend.queue, params['spec']) if operation == 'submit.preview' else register_held(backend, params['spec'], request_id)
    allowed = {'storage', 'request_id'} | ({'manifest'} if operation == 'transfer.begin' else {'manifest_digest'} if operation == 'transfer.publish' else set())
    _require(set(params) == allowed, 'Transfer parameters differ from the negotiated schema')
    identifier(params['request_id'], 'transfer request ID')
    _require(isinstance(params['storage'], str) and params['storage'] in backend.config['storages'], 'Unknown destination storage')
    if operation == 'transfer.begin':
        from .transfer import normalize_manifest
        params = {**params, 'manifest': normalize_manifest(params['manifest'])}
        _require(params['manifest'].get('node_id') == backend.config['node_id'] and params['manifest'].get('queue_id') == backend.queue['queue_id'], 'Transfer targets another node or queue')
        return begin_transfer(backend.config, params['storage'], params['request_id'], params['manifest'])
    receipt = lookup_transfer(backend.config, params['storage'], params['request_id'])
    _require(receipt.get('status') == 'NOT_FOUND' or receipt['queue_id'] == backend.queue['queue_id'],
             'Transfer belongs to another enrolled queue', 'WRONG_QUEUE')
    if operation == 'transfer.publish':
        return publish_transfer(backend.config, params['storage'], params['request_id'], params['manifest_digest'])
    return {'transfer': receipt}
