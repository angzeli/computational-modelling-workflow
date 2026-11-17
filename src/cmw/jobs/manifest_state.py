"""Executing-node state.json publisher for native manifests (#41 Phase 2b, #65).

``state.json`` is a projection of the existing queue record, never a second
lifecycle decision: the queue database stays the operational authority and
nothing here releases resources, unblocks dispatch or gates completion. The
Store publishes it after a committed transaction that saved a manifest job, so
every status transition is covered from one place. Writes are serialized by a
per-job lock, fenced to the registered attempt, revisioned, and atomic inside
``.cmw-manifest/``. The SCF classifier runs once, when the attempt first
reaches a terminal status; it is diagnostic only.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .manifests import MANIFEST_DIR, NODE_UUID, SCHEMA_VERSION, ManifestError, canonical

STATUS = {'Queue': 'queued', 'Hold': 'queued', 'Starting': 'queued', 'Run': 'running', 'Cancelling': 'running',
          'Done': 'done', 'Fail': 'failed', 'Cancelled': 'cancelled', 'Unknown': None}
TERMINAL = {'Done', 'Fail', 'Cancelled'}
RESTART_ROLES = ('WAVECAR', 'CHGCAR')
SCF_FIELDS = ('scf_result', 'last_step', 'ionic_step', 'energy_ev', 'delta_energy_ev',
              'total_magnetisation_mu_b', 'restart_outputs', 'hashes_pending', 'evidence')


def _utc(epoch):
    if not isinstance(epoch, (int, float)):
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(epoch)) + f'.{int(epoch % 1 * 1e6):06d}Z'


def _sidecar(job):
    """The job's own manifest directory, verified against the registered job.json digest."""
    manifest = job['manifest']
    sidecar = Path(job['cwd']) / MANIFEST_DIR
    if not stat.S_ISDIR(sidecar.lstat().st_mode) or sidecar.resolve(strict=True) != sidecar:
        raise ManifestError(f'{sidecar} must be a real directory inside the job')
    if hashlib.sha256((sidecar / 'job.json').read_bytes()).hexdigest() != manifest['job_sha256']:
        raise ManifestError(f'{sidecar / "job.json"} differs from the registered manifest')
    return sidecar


@contextmanager
def _locked(sidecar, name='state.lock'):
    fd = os.open(sidecar / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _scf(job):
    """Classify the managed VASP run directory once the attempt has terminated."""
    from cmw.periodic.vasp.scf_classifier import classify_run
    from .location_resolvers import vasp_runner_arguments
    fields = {'scf_result': 'unknown', 'last_step': None, 'ionic_step': None, 'energy_ev': None,
              'delta_energy_ev': None, 'total_magnetisation_mu_b': None, 'restart_outputs': {},
              'hashes_pending': False, 'evidence': []}
    if str(job['engine']).upper() != 'VASP':
        return fields
    try:
        route = vasp_runner_arguments(job)
    except (KeyError, TypeError, ValueError):
        route = None
    if route is None:
        fields['evidence'].append({'source': 'queue', 'marker': 'no managed VASP output directory', 'line': None})
        return fields
    output = route[2]
    outcar, stdout = output / 'OUTCAR', output / 'stdout.log'
    binding = (job.get('progress_sources') or {}).get('vasp_input') or {}
    # Bound only when registration proved this attempt's output absent and the attempt actually started.
    if (job.get('started_at') is None or binding.get('issue') is not None
            or binding.get('attempt_id') != job['attempt_id'] or binding.get('output') != str(output)):
        fields['evidence'].append({'source': 'queue', 'marker': 'run output is not bound to a started attempt '
                                   'with registration staging evidence', 'line': None})
        return fields
    for role in RESTART_ROLES:
        try:
            info = (output / role).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            # Evidenced output: its digest is computed later, never on the completion path.
            fields['restart_outputs'][role] = None
            fields['hashes_pending'] = True
    if not outcar.is_file():
        fields['evidence'].append({'source': 'OUTCAR', 'marker': 'OUTCAR is absent', 'line': None})
        return fields
    try:
        result = classify_run(outcar, stdout if stdout.is_file() else None, invocation_bound=True)
    except (OSError, ValueError) as exc:
        fields['evidence'].append({'source': 'OUTCAR', 'marker': f'unreadable: {str(exc)[:400]}', 'line': None})
        return fields
    return {**fields, **result}


def project(job, identity, previous=None):
    """The state.json document for this queue record (revision assigned by publish)."""
    native = job['status']
    if previous is not None and previous['native_status'] in TERMINAL and native == previous['native_status']:
        scf = {key: previous[key] for key in SCF_FIELDS}  # Classified once per terminal status.
    elif native in TERMINAL:
        scf = _scf(job)
    else:
        scf = {'scf_result': 'unknown', 'last_step': None, 'ionic_step': None, 'energy_ev': None,
               'delta_energy_ev': None, 'total_magnetisation_mu_b': None, 'restart_outputs': {},
               'hashes_pending': False, 'evidence': []}
    return {
        'schema_version': SCHEMA_VERSION, 'id': job['manifest']['id'], 'project': job['manifest']['project'],
        'provenance': 'native', 'revision': 0, 'writer_node': identity['node_id'],
        'status': STATUS.get(native), 'native_status': native if native in STATUS else None,
        'started_at': _utc(job.get('started_at')), 'ended_at': _utc(job.get('finished_at')),
        'exit_code': job.get('exit_code') if type(job.get('exit_code')) is int else None,
        # Only a recorded supported cancellation mode may be named; none is recorded yet.
        'cancel_mode': None,
        **scf,
        'binding': {'node_id': identity['node_id'], 'queue_id': identity['queue_id'],
                    'job_id': job['id'], 'attempt_id': job['attempt_id']},
    }


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _current(store, job_id):
    """Lock the job's sidecar, then read the committed record and the fenced previous state."""
    with store.read_transaction() as con:
        located = store.get(con, job_id)
    if not located.get('manifest'):
        yield None
        return
    identity = store.queue_identity() or {}
    if not isinstance(identity.get('node_id'), str) or not NODE_UUID.fullmatch(identity['node_id']):
        raise ManifestError('state.json needs a queue enrolled to a node UUID')
    sidecar = _sidecar(located)
    path = sidecar / 'state.json'
    with _locked(sidecar):
        # Read the committed record under the lock so publication order matches snapshot order.
        with store.read_transaction() as con:
            job = store.get(con, job_id)
        if job['attempt_id'] != located['attempt_id'] or job['cwd'] != located['cwd']:
            raise ManifestError('Job attempt changed while publishing state.json')
        previous = None
        if path.exists():
            previous = json.loads(path.read_text(encoding='utf-8'))
            if previous.get('binding') != {'node_id': identity['node_id'], 'queue_id': identity['queue_id'],
                                           'job_id': job['id'], 'attempt_id': job['attempt_id']}:
                raise ManifestError(f'{path} belongs to another attempt or writer; refusing to overwrite')
        yield job, identity, path, previous


def publish(store, job_id, *, launch_hashing=True):
    """Project the committed queue record into state.json; unchanged projections are not rewritten."""
    with _current(store, job_id) as current:
        if current is None:
            return None
        job, identity, path, previous = current
        state = project(job, identity, previous)
        if previous is not None:
            if {**previous, 'revision': 0} == state:
                return previous
            state['revision'] = previous['revision'] + 1
        else:
            state['revision'] = 1
        _write(path, state)
    if launch_hashing and state['hashes_pending'] and not (previous or {}).get('hashes_pending'):
        from .manifest_hashing import launch
        launch(store, job_id)  # Detached: never blocks drainage, Done or resource release.
    return state


def record_output_hashes(store, job_id, digests, findings=()):
    """Single-writer update of restart_outputs once asynchronous hashing finishes or fails."""
    with _current(store, job_id) as current:
        if current is None:
            raise ManifestError('Not a native manifest job')
        job, _, path, previous = current
        if previous is None or previous['native_status'] not in TERMINAL or previous['native_status'] != job['status']:
            raise ManifestError('Output hashes apply only to the current terminal state')
        outputs = dict(previous['restart_outputs'])
        for role, digest in digests.items():
            if role in outputs and outputs[role] is None:
                outputs[role] = digest
        state = {**previous, 'restart_outputs': outputs, 'hashes_pending': False,
                 'evidence': [*previous['evidence'], *findings], 'revision': previous['revision'] + 1}
        _write(path, state)
        return state
