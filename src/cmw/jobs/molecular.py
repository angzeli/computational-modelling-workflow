"""Reviewed molecular submissions: pure checks, explicit preparation and held registration.

Engine modules own scientific semantics. This module binds that reviewed plan to
one Jobs attempt; observing a job never prepares or finalizes scientific output.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from cmw.core.provenance import stable_hash
from cmw.progress.models import SourceDescriptor
from .runner_assets import file_identity
from .store import JobsError, Store, validate_note

_LIMIT = 256 * 1024
_FIELDS = {'schema', 'request_id', 'engine', 'name', 'intent', 'resources', 'workdir',
           'runtime', 'scientific', 'note'}


def normalize(spec):
    if not isinstance(spec, dict) or set(spec) - _FIELDS or not _FIELDS - {'note'} <= set(spec):
        raise JobsError('Molecular submission has missing or unexpected fields')
    if type(spec['schema']) is not int or spec['schema'] != 1 or spec['intent'] != 'held':
        raise JobsError('Molecular submission requires schema 1 and explicit held intent')
    if spec['engine'] not in {'ORCA', 'Multiwfn'}:
        raise JobsError('Reviewed local submission supports ORCA or Multiwfn')
    try:
        UUID(spec['request_id'])
    except (ValueError, TypeError, AttributeError) as exc:
        raise JobsError('request_id must be a UUID') from exc
    if not isinstance(spec['name'], str) or not spec['name'].strip() or len(spec['name']) > 256 or '\0' in spec['name']:
        raise JobsError('Name must be bounded nonempty literal text')
    path = spec['workdir']
    if not isinstance(path, str) or not Path(path).is_absolute() or '\0' in path or str(Path(path)) != path:
        raise JobsError('workdir must be an absolute canonical path')
    if Path(path).resolve(strict=False) != Path(path):
        raise JobsError('workdir cannot contain a symlink or traversal')
    resources = spec['resources']
    if not isinstance(resources, dict) or set(resources) != {'cpus', 'memory_gib', 'mpi_ranks', 'threads_per_rank'}:
        raise JobsError('Explicit CPUs, memory_gib, MPI ranks and threads_per_rank are required')
    for name, value in resources.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise JobsError(f'{name} must be positive and finite')
        if name != 'memory_gib' and type(value) is not int:
            raise JobsError(f'{name} must be an integer')
    if resources['cpus'] < resources['mpi_ranks'] * resources['threads_per_rank']:
        raise JobsError('CPU commitment understates rank/thread demand')
    if not isinstance(spec['scientific'], dict) or not isinstance(spec['runtime'], dict):
        raise JobsError('scientific and runtime must be explicit objects')
    result = {**spec, 'note': validate_note(spec.get('note', ''))}
    try:
        encoded = json.dumps(result, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise JobsError('Submission must contain finite JSON data') from exc
    if len(encoded.encode()) > _LIMIT:
        raise JobsError('Molecular submission exceeds 256 KiB')
    return result


def _engine(engine):
    if engine == 'ORCA':
        from cmw.molecular.orca import jobs
    elif engine == 'Multiwfn':
        from cmw.molecular.multiwfn import jobs
    else:
        raise JobsError('Unsupported molecular engine')
    return jobs


def check(spec):
    """Read-only: no Store access, preparation, discovery probes or execution."""
    try:
        spec = normalize(spec)
        result = _engine(spec['engine']).check(spec)
        if not isinstance(result, dict) or result.get('status') not in {'READY', 'ERROR', 'UNSUPPORTED', 'UNASSESSED'}:
            raise JobsError('Engine preflight returned an invalid report')
        return {**result, 'schema': 1, 'engine': spec['engine'], 'intent': 'Hold',
                'execution': 'Not authorized by checking or submission; release and dispatch are separate'}
    except (OSError, ValueError, KeyError, TypeError, JobsError) as exc:
        return {'schema': 1, 'status': 'ERROR', 'findings': [
            {'domain': 'submission', 'status': 'ERROR', 'message': str(exc)}]}


def _verified_dependencies(plan):
    dependencies = plan.get('dependencies', [])
    if not isinstance(dependencies, list) or not dependencies or len(dependencies) > 512:
        raise JobsError('Engine plan must declare a bounded nonempty dependency closure')
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not {'path', 'sha256'} <= set(dependency):
            raise JobsError('Malformed reviewed dependency')
        actual = file_identity(dependency['path'])
        if actual['sha256'] != dependency['sha256'] or (
                'size_bytes' in dependency and actual['size_bytes'] != dependency['size_bytes']):
            raise JobsError(f'Reviewed dependency changed: {dependency["path"]}')


def _plan(plan, spec):
    if not isinstance(plan, dict) or not {'argv', 'env', 'cwd', 'progress_sources', 'dependencies'} <= set(plan):
        raise JobsError('Engine plan is incomplete')
    if not isinstance(plan['argv'], list) or not plan['argv'] or any(not isinstance(v, str) or '\0' in v for v in plan['argv']):
        raise JobsError('Engine plan requires a literal argv')
    if not isinstance(plan['env'], dict) or any(not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', k)
            or not isinstance(v, str) or '\0' in v for k, v in plan['env'].items()):
        raise JobsError('Invalid engine environment')
    root, cwd = Path(spec['workdir']), Path(plan['cwd'])
    if not cwd.is_absolute() or not cwd.is_relative_to(root) or cwd.resolve() != cwd:
        raise JobsError('Engine working directory must remain inside the reviewed workdir')
    from .source_declarations import validate_sources
    sources = [SourceDescriptor(**entry) for entry in plan['progress_sources']]
    validate_sources(spec['engine'], sources, require_complete=bool(sources))
    # Foreground child sessions remain under Jobs ownership, including wrappers
    # that establish a private scientific child process group.
    plan = {**plan, 'env': {**plan['env'], 'CMW_JOBS_OWN_SESSION': '1'}}
    json.dumps(plan, allow_nan=False)
    return plan


def prepare(spec, checked=None):
    """Explicit materialization after a successful review; never called by check."""
    spec = normalize(spec)
    checked = check(spec) if checked is None else checked
    if checked.get('status') != 'READY':
        raise JobsError('Molecular preflight is not READY: ' + json.dumps(checked.get('findings', [])))
    _verified_dependencies(checked['plan'])
    root = Path(spec['workdir'])
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.exists():
        root.mkdir(mode=0o700)
    if root.is_symlink() or root.resolve() != root or not root.is_dir() or any(root.iterdir()):
        raise JobsError('Preparation requires a new empty workdir; existing evidence is retained')
    if root.stat().st_mode & 0o077:
        raise JobsError('Preparation workdir must be private (0700)')
    plan = _plan(_engine(spec['engine']).prepare(spec, checked), spec)
    _verified_dependencies(plan)
    return plan


def digest(spec):
    return stable_hash({key: value for key, value in normalize(spec).items() if key != 'request_id'})


def binding(spec, plan):
    spec, plan = normalize(spec), _plan(plan, spec)
    directories = {}
    for path in {spec['workdir'], plan['cwd']}:
        info = Path(path).stat()
        directories[path] = {'device': info.st_dev, 'inode': info.st_ino}
    result = {'schema': 1, 'request_id': spec['request_id'], 'digest': digest(spec),
              'spec': spec, 'plan': plan, 'directories': directories}
    if len(json.dumps(result, allow_nan=False).encode()) > _LIMIT:
        raise JobsError('Prepared submission binding exceeds 256 KiB')
    return result


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'request_id', 'digest', 'spec', 'plan', 'directories'}:
        raise JobsError('Malformed molecular submission binding')
    spec = normalize(value['spec'])
    if value['schema'] != 1 or value['request_id'] != spec['request_id'] or value['digest'] != digest(spec):
        raise JobsError('Molecular binding identity differs from its reviewed specification')
    _plan(value['plan'], spec)
    if len(json.dumps(value, allow_nan=False).encode()) > _LIMIT:
        raise JobsError('Molecular submission binding exceeds 256 KiB')
    return value


def _stored_argv(argv):
    if Path(argv[0]).name in {'bash', 'sh', 'zsh'}:
        return [argv[0], '-e', *([] if Path(argv[0]).name == 'sh' else ['-o', 'pipefail']), *argv[1:]]
    return argv


def launch_blocker(job, *, final=False):
    value = job.get('molecular_submission')
    if value is None:
        return None
    try:
        value = validate_binding(value)
        spec, plan = value['spec'], value['plan']
        if (job['engine'] != spec['engine'] or job['resources'] != spec['resources']
                or job['argv'] != _stored_argv(plan['argv']) or job['env'] != plan['env'] or job['cwd'] != plan['cwd']):
            raise JobsError('Jobs execution differs from its reviewed molecular plan')
        for path, expected in value['directories'].items():
            directory = Path(path)
            info = directory.stat()
            if directory.resolve() != directory or not directory.is_dir() or {'device': info.st_dev, 'inode': info.st_ino} != expected:
                raise JobsError('Prepared working directory identity changed')
        if final:
            _verified_dependencies(plan)
        return None
    except (OSError, ValueError, KeyError, TypeError, JobsError) as exc:
        return 'Molecular launch preflight: ' + str(exc)


def lookup_request(store, request_id):
    """No migration or queue creation during a retry lookup."""
    if not store.path.is_file():
        return None
    con = sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        Store.control(con)
        for job in Store.rows(con):
            if job.get('molecular_submission', {}).get('request_id') == request_id:
                return job
    finally:
        con.close()
    return None


def submit(store, spec, *, preview=False):
    spec = normalize(spec)
    if not preview:
        previous = lookup_request(store, spec['request_id'])
        if previous is not None:
            if previous['molecular_submission']['digest'] != digest(spec):
                raise JobsError('Request ID already belongs to a different molecular submission')
            return {'status': 'REGISTERED', 'replayed': True, 'job': previous}
    checked = check(spec)
    if preview or checked['status'] != 'READY':
        return checked
    plan = prepare(spec, checked)
    value = binding(spec, plan)
    # Retain the exact review if queue registration fails after preparation.
    evidence = Path(spec['workdir']) / 'cmw-submission.json'
    with evidence.open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
    job = store.add(argv=plan['argv'], env=plan['env'], cwd=plan['cwd'], name=spec['name'],
                    engine=spec['engine'], **spec['resources'], hold=True, note=spec['note'],
                    progress_sources=tuple(SourceDescriptor(**source) for source in plan['progress_sources']),
                    molecular_submission=value)
    return {'status': 'REGISTERED', 'replayed': False, 'job': job}


def handle(args):
    from .locations import read_location_metadata
    try:
        spec = read_location_metadata(args.spec, max_bytes=_LIMIT)
        result = check(spec) if args.jobs_operation == 'check' else submit(Store(args.state), spec, preview=args.preview)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0 if result['status'] in {'READY', 'REGISTERED'} else 2
    except (OSError, ValueError, TypeError, KeyError, JobsError) as exc:
        print(json.dumps({'status': 'ERROR', 'message': str(exc)}))
        return 2


def locations(job):
    """Generic declarations from the immutable engine plan; no output parsing."""
    from .locations import LocationDescriptor
    value = job.get('molecular_submission')
    if not isinstance(value, dict):
        return ()
    plan = value.get('plan', {})
    declarations = [LocationDescriptor('output', value['spec']['workdir'],
                      origin='reviewed-engine-plan', qualification='declared output; validation is separate')]
    for field, role, kind in (('attempt_directory', 'scratch', 'directory'),
                              ('result_path', 'result', 'file'),
                              ('prepared_path', 'records', 'file'),
                              ('source_result', 'source-result', 'file')):
        if isinstance(plan.get(field), str):
            declarations.append(LocationDescriptor(role, plan[field], kind=kind,
                origin='reviewed-engine-plan', qualification='exact engine plan declaration', expected_creation=True))
    for source in plan.get('progress_sources', []):
        declarations.append(LocationDescriptor('scientific-source', source['path'], kind='file',
            stage=source.get('stage'), origin='reviewed-engine-plan', qualification=source['role'], expected_creation=True))
    return tuple(declarations)


def inspect_result(job):
    """Expose the existing finalizer record on explicit inspection, without finalizing."""
    from .locations import read_location_metadata
    value = job.get('molecular_submission')
    if not isinstance(value, dict):
        return None
    path = value.get('plan', {}).get('result_path')
    result = {'jobs_process_outcome': job.get('status'), 'jobs_exit_code': job.get('exit_code'),
              'path': path, 'record': None, 'status': 'PENDING',
              'qualification': 'Recorded finalizer evidence; this inspection neither finalizes nor revalidates artifact bytes. '
                               'Process outcome, native completion, output identity, scientific validation and reuse remain separate.'}
    if job.get('status') not in {'Done', 'Fail', 'Cancelled'}:
        return result
    if not isinstance(path, str) or not Path(path).is_absolute():
        return {**result, 'status': 'UNAVAILABLE', 'reason': 'No exact finalizer record was declared'}
    try:
        record = read_location_metadata(path)
        expected = value['plan'].get('scientific_association', {}).get('target_id')
        if expected and record.get('target', {}).get('target_id') != expected:
            raise ValueError('Finalizer target differs from the reviewed scientific target')
        return {**result, 'status': 'RECORDED', 'record': record}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, 'status': 'UNAVAILABLE', 'reason': str(exc)}
