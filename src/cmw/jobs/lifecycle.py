"""Bounded, read-only evidence for a recorded managed attempt, never authority.

Only recorded worker, controller and domain-anchor PIDs are inspected. Legacy
state cannot enumerate a complete owned domain or bind terminal receipts, so
neither process absence nor a receipt authorizes a terminal transition.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time
from typing import Any

import psutil

from . import ownership
from .store import ACTIVE, JobsError

FRESH_SECONDS = 5
RECEIPT_LIMIT = 4096
LIFECYCLE_FIELDS = ('id', 'attempt_id', 'status', 'claim', 'claimed_at', 'worker',
                    'group', 'started_at', 'finished_at', 'exit_code', 'cancel_requested',
                    'worker_protocol', 'termination_requested')
SUSPENSION_FIELDS = ('backend', 'protocol', 'phase', 'desired', 'revision', 'request',
                     'reason', 'inhibited', 'capability', 'clock')


@dataclass(frozen=True)
class Evidence:
    verdict: str
    reason: str
    code: str = ''
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {'verdict': self.verdict, 'reason': self.reason, 'code': self.code,
                **deepcopy(self.details)}


@dataclass(frozen=True)
class ExecutionObservation:
    job_id: str
    attempt_id: str | None
    claim: str | None
    persisted_status: str
    observed_execution: str
    observation_quality: str
    observed_at: float
    state_identity: dict[str, Any]
    evidence: dict[str, Evidence]
    limitations: tuple[str, ...]
    reconciliation_required: bool
    summary: str

    def to_dict(self, *, now=None):
        age = max(0, (time.time() if now is None else now) - self.observed_at)
        return {'schema': 1, 'advisory': True, 'job_id': self.job_id,
                'attempt_id': self.attempt_id, 'claim': self.claim,
                'persisted_status': self.persisted_status,
                'observed_execution': self.observed_execution,
                'observation_quality': self.observation_quality,
                'observed_at': self.observed_at, 'age_seconds': age,
                'stale': age > FRESH_SECONDS, 'state_identity': deepcopy(self.state_identity),
                'evidence': {key: value.to_dict() for key, value in self.evidence.items()},
                'limitations': list(self.limitations),
                'reconciliation_required': self.reconciliation_required,
                'summary': self.summary}


def _inode(path):
    try:
        info = path.lstat()
        return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]
    except OSError:
        return None


def _state_identity(store):
    return {'root': str(store.root), 'database': str(store.path),
            'root_inode': _inode(store.root), 'database_inode': _inode(store.path)}


def _suspension_binding(value):
    if not isinstance(value, dict):
        return value
    evidence = value.get('evidence')
    if isinstance(evidence, dict):
        evidence = {key: item for key, item in evidence.items()
                    if key not in {'checked_clock', 'observed_at'}}
    return {**{key: value.get(key) for key in SUSPENSION_FIELDS},
            'verification_present': value.get('verified_at') is not None,
            'domain_evidence': evidence}


def _lifecycle(job):
    return {**{key: job.get(key) for key in LIFECYCLE_FIELDS},
            'owned_session': job.get('env', {}).get('CMW_JOBS_OWN_SESSION'),
            'suspension': _suspension_binding(job.get('suspension'))}



def observation_key(store, job):
    """Cheap UI/cache binding; notes and output/progress are deliberately absent."""
    return (json.dumps(_state_identity(store), sort_keys=True),
            json.dumps(_lifecycle(job), sort_keys=True))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def observe_process(owner):
    """Recheck birth around status; access failure never means process absence."""
    if not isinstance(owner, dict) or not {'pid', 'birth', 'host', 'boot'} <= owner.keys():
        return Evidence('missing', 'No complete recorded process identity', 'identity_missing')
    if (type(owner['pid']) is not int or owner['pid'] <= 0 or
            not _finite(owner['birth']) or not _finite(owner['boot']) or
            not isinstance(owner['host'], str)):
        return Evidence('unavailable', 'Invalid recorded process identity', 'identity_invalid')
    details = {'recorded_identity': deepcopy(owner)}
    try:
        if owner['host'] != socket.gethostname():
            return Evidence('mismatch', 'Recorded host differs from this host', 'host_mismatch', details)
        if owner['boot'] != psutil.boot_time():
            return Evidence('mismatch', 'Recorded boot differs from this boot', 'boot_mismatch', details)
        current = ownership.identity(owner['pid'])
        details['observed_identity'] = current
        if current != owner:
            return Evidence('mismatch', 'Recorded PID birth identity differs', 'pid_reused', details)
        status = psutil.Process(owner['pid']).status()
        details['process_status'] = status
        if ownership.identity(owner['pid']) != current:
            return Evidence('mismatch', 'Process identity changed during observation', 'identity_changed', details)
        if status in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return Evidence('gone', 'Recorded process is zombie/dead, not a live worker', status, details)
        return Evidence('matched', 'Recorded process birth identity matches',
                        'stopped' if status in (psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP) else 'live', details)
    except psutil.ZombieProcess:
        return Evidence('unavailable', 'Zombie PID birth identity could not be verified', 'zombie_identity_unverified', details)
    except (psutil.NoSuchProcess, ProcessLookupError):
        return Evidence('gone', 'Recorded PID is absent', 'absent', details)
    except (psutil.Error, OSError):
        return Evidence('unavailable', 'Process identity/status is inaccessible', 'inaccessible', details)


@contextmanager
def _read_file(store, parts):
    """Pin ordinary path components without following symlinks or creating files.

    Revalidate names against the pinned descriptors after inspection, including
    directories: a replacement cannot silently redirect a lock or receipt.
    """
    handles, identities = [], []
    try:
        root_fd = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(root_fd)
        root = os.fstat(root_fd)
        if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) & 0o077:
            raise JobsError('State directory is not private and owned by this user')
        identities.append((None, store.root, root))
        parent = root_fd
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts)-1:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=parent)
            handles.append(descriptor)
            info = os.fstat(descriptor)
            expected = stat.S_ISREG if index == len(parts)-1 else stat.S_ISDIR
            if not expected(info.st_mode) or info.st_uid != os.geteuid():
                raise JobsError('Evidence path is not an owned ordinary file/directory')
            identities.append((parent, part, info))
            parent = descriptor
        yield handles[-1]
        for parent, name, original in identities:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise JobsError('Evidence path disappeared during observation') from exc
            if (original.st_dev, original.st_ino, original.st_mode) != (current.st_dev, current.st_ino, current.st_mode):
                raise JobsError('Evidence path identity changed during observation')
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)


def _attempt_parts(attempt_id, filename):
    if not isinstance(attempt_id, str) or re.fullmatch('[0-9a-f]{32}', attempt_id) is None:
        raise JobsError('Invalid recorded attempt identity')
    return ('attempts', attempt_id, filename)


def _observe_lock_path(store, parts):
    try:
        with _read_file(store, parts) as descriptor:
            info = os.fstat(descriptor)
            details = {'device': info.st_dev, 'inode': info.st_ino}
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = Evidence('held', 'Existing lock is held', 'held', details)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                result = Evidence('unheld', 'Existing lock is unheld', 'unheld', details)
        return result
    except FileNotFoundError:
        return Evidence('missing', 'Recorded lock path is missing', 'missing')
    except (OSError, JobsError):
        return Evidence('unavailable', 'Lock path inaccessible, redirected or changed', 'unsafe_or_changed')


def observe_lock(store, attempt_id):
    try:
        parts = _attempt_parts(attempt_id, 'worker.lock')
    except JobsError:
        return Evidence('unavailable', 'Invalid recorded attempt identity', 'identity_invalid')
    return _observe_lock_path(store, parts)


def _observe_domain(job, worker, lock):
    anchor = observe_process(job.get('group'))
    details = {'anchor': anchor.to_dict(), 'membership_complete': False,
               'inspected_pids': [job['group']['pid']] if anchor.verdict == 'matched' else [],
               'scope': 'recorded anchor only; no process enumeration or drainage proof'}
    if anchor.verdict != 'matched':
        return Evidence('partial', 'Recorded domain anchor cannot be verified; surviving ranks are unresolved',
                        'anchor_unverified', details)
    try:
        pid = job['group']['pid']
        pgid, sid = os.getpgid(pid), os.getsid(pid)
        again = observe_process(job['group'])
        details.update(pgid=pgid, sid=sid)
        if again.verdict != 'matched' or pgid != pid or sid != pid:
            return Evidence('partial', 'Recorded domain anchor identity/topology changed', 'anchor_changed', details)
        if worker.verdict != 'matched' or lock.verdict != 'held':
            return Evidence('partial', 'Recorded anchor matches but guardian ownership is unverified', 'guardian_unverified', details)
        return Evidence('anchor_verified', 'Recorded domain anchor birth and session/group match', 'anchor_verified', details)
    except (OSError, psutil.Error):
        return Evidence('unavailable', 'Recorded domain topology is inaccessible', 'inaccessible', details)


def _observe_receipt(store, job, worker, now):
    try:
        parts = _attempt_parts(job.get('attempt_id'), 'payload-exit.json')
        with _read_file(store, parts) as descriptor:
            before = os.fstat(descriptor)
            payload = os.read(descriptor, RECEIPT_LIMIT + 1)
            after = os.fstat(descriptor)
            if len(payload) > RECEIPT_LIMIT or before.st_size > RECEIPT_LIMIT:
                return Evidence('unavailable', 'Receipt exceeds bounded read limit', 'oversized')
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise JobsError('Receipt changed while reading')
        receipt = json.loads(payload)
        if (not isinstance(receipt, dict) or type(receipt.get('exit_code')) is not int or
                not _finite(receipt.get('recorded_at'))):
            raise ValueError('Malformed receipt')
        details = {'exit_code': receipt['exit_code'], 'recorded_at': receipt['recorded_at'],
                   'terminal_authority': False, 'attempt_binding': 'path_only'}
        started = job.get('started_at') or job.get('claimed_at')
        if not _finite(started) or not started <= receipt['recorded_at'] <= now:
            return Evidence('stale', 'Receipt time is outside this recorded attempt', 'time_unbound', details)
        if job.get('exit_code') is not None and job['exit_code'] != receipt['exit_code']:
            return Evidence('contradictory', 'Receipt exit code contradicts persisted exit code', 'exit_conflict', details)
        return Evidence('unbound', 'Legacy receipt lacks immutable claim binding and complete domain drainage',
                        'legacy_receipt', details)
    except FileNotFoundError:
        return Evidence('missing', 'No recorded attempt-local payload receipt', 'missing')
    except (OSError, JobsError):
        return Evidence('unavailable', 'Receipt path inaccessible, redirected or changed', 'unsafe_or_changed')
    except (ValueError, UnicodeError):
        return Evidence('malformed', 'Receipt is not a supported payload-exit record', 'malformed')


def _read_job(store, job_id):
    """One bounded read transaction; never migrate, create or repair state."""
    with _read_file(store, ('queue.sqlite3',)) as descriptor:
        # CMW schema 1/2/3/4 uses rollback journalling. Refuse WAL rather than let a
        # read-only SQLite connection create shared-memory sidecars.
        header = os.pread(descriptor, 20, 0)
        if len(header) < 20 or header[18:20] != b'\x01\x01':
            raise JobsError('Unsupported database journal format for read-only observation')
        con = sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True, timeout=0.1)
        try:
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            if type(control.get('schema')) is not int or control['schema'] not in {1, 2, 3, 4}:
                raise JobsError('Unsupported Jobs state schema')
            if control['schema'] == 4:
                store._queue_identity(con, control['schema'])
            if (control['schema'] >= 3 and control.get('runtime_protocol') is not None and
                    (type(control['runtime_protocol']) is not int or control['runtime_protocol'] != 3)):
                raise JobsError('Unsupported Jobs controller runtime protocol')
            if type(job_id) is int:
                row = con.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    raise JobsError('Recorded job is missing')
                job = json.loads(row[0])
                if not isinstance(job, dict) or job.get('id') != job_id:
                    raise JobsError('Recorded job identity differs')
            else:
                # Reuse established display-label and attempt validation. This
                # reads one job and never constructs a history-wide snapshot.
                job = store.get(con, job_id)
        finally:
            con.close()
    return job, control


def read_job(store, job_id):
    """Read one existing job/control record without scanning process or history."""
    try:
        return _read_job(store, job_id)
    except JobsError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        raise JobsError('Jobs observation source is malformed or unavailable') from exc


def _observe_controller(store, control, now):
    owner = observe_process(control.get('owner'))
    lock = _observe_lock_path(store, ('controller.lock',))
    details = {'owner': owner.to_dict(), 'lock': lock.to_dict(),
               'heartbeat': control.get('heartbeat'), 'dispatch': control.get('dispatch')}
    if owner.verdict == 'matched' and lock.verdict == 'held':
        heartbeat = control.get('heartbeat')
        stale = not _finite(heartbeat) or not 0 <= now-heartbeat <= FRESH_SECONDS
        return Evidence('stale' if stale else 'online', 'Recorded controller authority is present', 'authority_present', details)
    if owner.verdict in {'missing', 'gone', 'mismatch'} and lock.verdict in {'missing', 'unheld'}:
        return Evidence('offline', 'No matching controller owner and held lock observed', 'offline', details)
    return Evidence('unavailable', 'Controller authority cannot be excluded', 'authority_uncertain', details)


def _observe_suspension(job, schema):
    """Project persisted P2 constraints without calling its mutating coordinator.

    A matching worker/anchor cannot prove suspension, release or full-domain
    drainage. Worker receipts and IPC remain under the original P2 authority.
    """
    value = job.get('suspension')
    worker_protocol = job.get('worker_protocol')
    if worker_protocol is not None and (type(worker_protocol) is not int or
            worker_protocol not in ({3} if schema >= 3 else {2, 3})):
        return Evidence('unsupported', 'Recorded worker runtime protocol is unsupported',
                        'unsupported_worker', {'worker_protocol': worker_protocol})
    if value is None:
        return Evidence('not_applicable', 'Attempt is not enrolled for controlled suspension')
    if not isinstance(value, dict):
        return Evidence('unsupported', 'Invalid recorded suspension metadata', 'unsupported_suspension')
    from .suspension import BACKENDS, PROTOCOL

    details = {key: deepcopy(value.get(key)) for key in
               ('phase', 'desired', 'revision', 'request', 'inhibited', 'backend', 'protocol', 'verified_at')}
    details.update(worker_protocol=job.get('worker_protocol'), stored_reason=value.get('reason', ''),
                   scope='persisted suspension metadata; no IPC/drainage revalidation')
    if (schema not in {3, 4} or value.get('backend') not in BACKENDS or
            type(value.get('protocol')) is not int or value['protocol'] != PROTOCOL or
            value.get('phase') not in {'Run', 'Suspending', 'Suspended', 'Resuming', 'Unverified'} or
            value.get('desired') not in {'Run', 'Suspended', 'Terminated'} or
            type(value.get('revision')) is not int or value['revision'] < 0 or
            type(value.get('inhibited')) is not bool or
            (job.get('worker') is not None and job.get('worker_protocol') != 3)):
        return Evidence('unsupported', 'Recorded suspension schema/protocol is unsupported or incomplete',
                        'unsupported_suspension', details)
    capability = value.get('capability')
    if capability is not None and (not isinstance(capability, dict) or
            type(capability.get('protocol')) is not int or capability['protocol'] != PROTOCOL):
        return Evidence('unsupported', 'Recorded runner capability protocol is unsupported',
                        'unsupported_runner', details)
    if value['phase'] == 'Unverified':
        return Evidence('unverified', 'Persisted suspension remains Unverified; worker observation cannot override it',
                        'suspension_unverified', details)
    if value['inhibited'] or job.get('cancel_requested') or job.get('termination_requested'):
        return Evidence('inhibited', 'Persisted control/termination constraints remain in force',
                        'control_inhibited', details)
    return Evidence('stored', 'Suspension phase is persisted metadata, not a fresh computation-state claim',
                    'stored_phase', details)


def observe_execution(store, job, *, state=None, now=None):
    """Observe one selected attempt; results never authorize lifecycle mutation.

    ``state`` is accepted for presentation compatibility, but raw schema and
    binding are independently reread because snapshot projections normalize old
    schemas. Lifecycle changes during the probes invalidate the whole result.
    """
    observed_at = time.time() if now is None else now
    evidence = {key: Evidence('unavailable', 'Observation has not been established', 'not_observed')
                for key in ('source', 'worker', 'worker_lock', 'domain', 'receipt', 'controller', 'suspension')}
    limits = ('Legacy domain membership is incomplete; surviving MPI ranks are not excluded.',
              'Worker/lock evidence is not scientific progress or scientific acceptance.',
              'Legacy terminal receipts provide no recovery authority; no state was changed.')
    category, quality, required = 'OBSERVATION_UNAVAILABLE', 'unavailable', job.get('status') in ACTIVE
    summary = 'Stored status is unverified; lifecycle observation unavailable'
    binding = observation_key(store, job)
    identity = json.loads(binding[0])
    try:
        current, control = read_job(store, job['id'])
        if observation_key(store, current) != binding:
            raise JobsError('Lifecycle binding changed before observation')
        evidence['source'] = Evidence('matched', 'Canonical state and recorded attempt match', 'bound', {'schema': control['schema']})
        suspension = evidence['suspension'] = _observe_suspension(job, control['schema'])
        if job.get('suspension') is not None:
            limits += ('Suspension phase is persisted evidence; worker/anchor checks do not validate IPC, rank stop state or RELEASE.',)
        if job['status'] not in ACTIVE:
            category, quality, required = 'NOT_APPLICABLE', 'not_applicable', False
            summary = 'No active managed attempt; persisted terminal/pending status is unchanged'
        else:
            worker = evidence['worker'] = observe_process(job.get('worker'))
            lock = evidence['worker_lock'] = observe_lock(store, job.get('attempt_id'))
            domain = evidence['domain'] = _observe_domain(job, worker, lock)
            receipt = evidence['receipt'] = _observe_receipt(store, job, worker, observed_at)
            evidence['controller'] = _observe_controller(store, control, observed_at)
            quality = 'partial'
            if worker.verdict == 'matched' and lock.verdict == 'held' and domain.verdict == 'anchor_verified':
                category = 'VERIFIED_STOPPED' if worker.code == 'stopped' else 'VERIFIED_WORKER'
                summary = 'Recorded worker verified; recorded domain anchor verified; full domain not established'
                if category == 'VERIFIED_STOPPED':
                    summary = 'Recorded worker verified STOPPED; full domain state not established'
                required = job['status'] == 'Unknown'
            elif worker.verdict == 'gone':
                category = 'WORKER_MISSING'
                summary = 'Stored status is UNVERIFIED; recorded worker absent/dead; reconciliation required'
            elif worker.verdict == 'matched':
                category = 'PARTIAL_DOMAIN'
                summary = 'Worker identity matches; ownership/domain UNVERIFIED; reconciliation required'
            elif worker.verdict == 'unavailable':
                category, quality = 'OBSERVATION_UNAVAILABLE', 'unavailable'
                summary = 'Execution observation unavailable; stored status UNVERIFIED; review required'
            else:
                category = 'OWNERSHIP_UNVERIFIED'
                summary = 'Stored status UNVERIFIED; recorded ownership does not match; reconciliation required'
            if suspension.verdict == 'unsupported':
                category, quality, required = 'OBSERVATION_UNAVAILABLE', 'unavailable', True
                summary = 'Execution observation unavailable; recorded suspension protocol is unsupported'
            elif (suspension.verdict != 'not_applicable' and
                  (worker.code == 'stopped' or domain.details.get('anchor', {}).get('code') == 'stopped')):
                category, required = 'OWNERSHIP_UNVERIFIED', True
                summary = 'Protected worker/anchor observed STOPPED; controlled execution is UNVERIFIED'
            elif suspension.verdict == 'unverified':
                category, required = 'OWNERSHIP_UNVERIFIED', True
                summary = 'Persisted suspension is UNVERIFIED; worker/anchor evidence cannot resolve it'
            elif (category in {'VERIFIED_WORKER', 'VERIFIED_STOPPED'} and
                  (suspension.verdict == 'inhibited' or
                   suspension.details.get('phase', 'Run') != 'Run')):
                category = 'CONTROLLED_PHASE'
                summary = ('Worker/anchor verified within scope; stored suspension/control constraints remain in force; '
                           'computational phase is not independently verified')
            if receipt.verdict in {'unbound', 'stale', 'contradictory', 'malformed'}:
                limits += ('Receipt evidence cannot establish Done, Fail or Cancelled.',)
        if evidence['worker'].verdict == 'matched':
            fresh_worker = observe_process(job.get('worker'))
            if ((fresh_worker.verdict, fresh_worker.code) !=
                    (evidence['worker'].verdict, evidence['worker'].code) or
                    observe_lock(store, job.get('attempt_id')).to_dict() != evidence['worker_lock'].to_dict()):
                raise JobsError('Worker/lock evidence changed during observation')
        fresh, fresh_control = read_job(store, job['id'])
        control_binding = lambda value: {key: value.get(key) for key in ('schema', 'runtime_protocol', 'owner', 'dispatch', 'stop')}
        if (observation_key(store, fresh) != binding or
                control_binding(fresh_control) != control_binding(control)):
            raise JobsError('Lifecycle binding changed during observation')
    except (JobsError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        evidence['source'] = Evidence('unavailable', 'Source/schema inaccessible, unsupported or changed; request fresh observation', 'source_unverified')
        category, quality = 'OBSERVATION_UNAVAILABLE', 'unavailable'
        required = job.get('status') in ACTIVE
        summary = 'Stored status UNVERIFIED; observation source unsupported, unavailable or changed'
    return ExecutionObservation(str(job.get('display_id', job.get('id'))), job.get('attempt_id'),
                                job.get('claim'), job.get('status', 'Unknown'), category, quality,
                                observed_at, identity, evidence, limits, required, summary)
