"""Opt-in execution control. Clients persist intent; only the owning worker signals.

Canonical Run remains active throughout a suspension. Schema 3 prevents older
workers from mistaking the orthogonal metadata for a dispatch opportunity.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import time
from uuid import uuid4

import psutil

from .ownership import (identity, owner_alive, lock_held, group_members,
                        snapshot_owned_session, group_exists)
from .store import ACTIVE, JobsError, validate_note

BACKENDS = ('local-command-v1', 'vasp-port-v1', 'orca-local-v1')
PROTOCOL = 1
TRANSITION_TIMEOUT = 6.0
VERIFY_CADENCE = 1.0

class DomainPending(JobsError):
    """An enrolled computational domain is changing; it is not signal-ready."""


class RequestSuperseded(JobsError):
    """A newer accepted request requires the worker's next observation."""


BINDING = ('protocol', 'backend', 'job_id', 'attempt_id', 'claim', 'worker',
           'anchor', 'state_identity', 'nonce', 'directory_identity', 'clock')


def initial_state(backend):
    return dict(backend=backend, protocol=PROTOCOL, phase='Run', desired='Run',
                revision=0, request=None, reason='', inhibited=False,
                capability=None, evidence=None, verified_at=None, intervals=[],
                suspended_seconds=0.0, unsuspended_elapsed=None)


def enroll_state(store, con):
    """Migration is opt-in and cannot invalidate a live legacy supervisor."""
    control = store.control(con)
    if control['schema'] >= 3:
        return
    if (lock_held(store.root/'controller.lock') or owner_alive(control.get('owner'))):
        if control.get('runtime_protocol') != 3:
            raise JobsError('Legacy controller still owns this state; stop it with its original runtime before enrollment')
    if any(j['status'] in ACTIVE and j.get('worker_protocol') != 3 for j in store.rows(con)):
        raise JobsError('Legacy active or unresolved attempt blocks suspension schema migration')
    control['schema'] = 3
    store.set_control(con, control)


def _file_identity(path):
    info = Path(path).stat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def _authority(job):
    return {'claim': job.get('claim'), 'worker': job.get('worker')}


def _alive_authority(store, job):
    return (job.get('worker_protocol') == 3 and owner_alive(job.get('worker'))
            and lock_held(store.root/'attempts'/job['attempt_id']/'worker.lock')
            and owner_alive(job.get('group')))


def _select(state, job_id):
    for job in state['jobs']:
        if str(job_id) in {str(job['id']), job['display_id'], 'J'+str(job['id'])}:
            return job
    raise JobsError(f'No such job: {job_id}')


def _storage(records):
    if not records:
        raise JobsError('Required storage was not registered')
    for record in records:
        path = Path(record['path'])
        if not path.is_dir() or _file_identity(path) != {k: record[k] for k in ('device', 'inode')}:
            raise JobsError(f'Required storage unavailable or replaced: {path}')


def readiness(store, job_id, action=None):
    """Non-mutating projection; no initialization, migration, launch or signal."""
    state = store.snapshot()
    job = _select(state, job_id)
    suspension = job.get('suspension') or {}
    phase = suspension.get('phase', 'Run')
    action = action or ('continue' if phase == 'Suspended' else 'suspend')
    if action not in {'suspend', 'continue'}:
        raise JobsError('Action must be suspend or continue')
    capability = suspension.get('capability') or {}
    reasons = []
    supported = (suspension.get('backend') in BACKENDS and capability.get('protocol') == PROTOCOL)
    if not supported:
        reasons.append("Suspension unavailable for this attempt's runtime. Start a future job with the qualified suspension contract.")
    if job['status'] != 'Run' or job.get('cancel_requested') or job.get('termination_requested'):
        reasons.append('Attempt is not running or termination has precedence')
    if not _alive_authority(store, job):
        reasons.append('Matching execution worker or protected ownership anchor is unavailable; no adoption permitted')
    expected = 'Run' if action == 'suspend' else 'Suspended'
    if phase != expected:
        reasons.append(f'{action.capitalize()} requires verified {expected}; current state is {phase}')
    if supported:
        try:
            _storage(capability.get('storage'))
            if capability.get('state_identity') != _file_identity(store.path):
                raise JobsError('Jobs state identity differs from enrolled runtime')
            if not capability.get('deadline', {}).get('known'):
                raise JobsError('Runner deadline handling is unknown')
            evidence = suspension.get('evidence') or {}
            from .suspension_protocol import clock_descriptor, now_clock
            if suspension.get('clock') != clock_descriptor():
                raise JobsError('Clock or boot binding changed')
            if not isinstance(evidence.get('checked_clock'), (int, float)) or not 0 <= now_clock()-evidence['checked_clock'] <= 3:
                raise JobsError('Fresh worker verification is required')
            if not owner_alive(capability.get('runner')):
                raise JobsError('Coordinated runner no longer exists')
        except (JobsError, OSError, KeyError, ValueError) as exc:
            reasons.append(str(exc))
    return dict(supported=supported, ready=not reasons, reasons=reasons,
                job_id=job['id'], display_id=job['display_id'], name=job['name'],
                attempt_id=job['attempt_id'], action=action, revision=suspension.get('revision', 0),
                phase=phase, authority=_authority(job), backend=suspension.get('backend'),
                protocol=suspension.get('protocol'), computational_scope=capability.get('computational_scope', []),
                protected_scope=capability.get('protected_scope', []),
                deadline=capability.get('deadline', {}), storage=capability.get('storage', []),
                resources=job['resources'], qualification=capability.get('qualification', {
                    'engine': False, 'sleep_wake': False, 'native_mpi': 'not established for this runtime'}),
                other_active=[{'display_id': j['display_id'], 'name': j['name'],
                               'status': j['status'], 'phase': (j.get('suspension') or {}).get('phase', 'Run')}
                              for j in state['jobs'] if j['id'] != job['id'] and j['status'] in ACTIVE],
                reason=suspension.get('reason', ''))


def project_timing(job):
    """Project operational durations only while the worker's evidence is fresh."""
    value = job.get('suspension')
    if not value:
        return
    value['current_suspended_seconds'] = None
    value['verification_fresh'] = False
    if value.get('clock_finished') is not None:
        return
    try:
        from .suspension_protocol import now_clock, clock_descriptor
        now = now_clock()
        started = value.get('clock_started')
        evidence = value.get('evidence') or {}
        fresh = 0 <= now-evidence['checked_clock'] <= 3
        if value.get('clock') != clock_descriptor() or not isinstance(started, (int, float)) or now < started or not fresh or value.get('phase') == 'Unverified' or job.get('status') == 'Unknown' or not owner_alive(job.get('worker')):
            raise ValueError('Operational time continuity unavailable')
        value['verification_fresh'] = True
        total = 0.0
        for interval in value.get('intervals', []):
            end = interval.get('end')
            duration = (now if end is None else end)-interval['start']
            if duration < 0:
                raise ValueError('Invalid monotonic interval')
            total += duration
            if end is None:
                value['current_suspended_seconds'] = duration
        value['suspended_seconds'] = total
        value['unsuspended_elapsed'] = max(0.0, now-started-total)
    except (ValueError, KeyError, TypeError, OSError):
        value['unsuspended_elapsed'] = None


def finalize_timing(job):
    """Freeze operational accounting at genuine terminal evidence, not a stop."""
    value = job.get('suspension')
    if not value:
        return
    try:
        from .suspension_protocol import now_clock, clock_descriptor
        end = now_clock()
        start = value.get('clock_started')
        if value.get('clock') != clock_descriptor() or start is None or end < start or value.get('accounting_uncertain'):
            raise ValueError('Final operational time is uncertain')
        intervals = value.get('intervals', [])
        if any(item.get('end') is None for item in intervals):
            raise ValueError('Unclosed interval at terminal observation')
        total = sum(item['end']-item['start'] for item in intervals)
        if total < 0 or total > end-start:
            raise ValueError('Invalid final operational duration')
        value.update(clock_finished=end, suspended_seconds=total,
                     unsuspended_elapsed=end-start-total, verification_fresh=False)
    except (ValueError, KeyError, TypeError, OSError):
        value.update(unsuspended_elapsed=None, accounting_uncertain=True, verification_fresh=False)


def submit(store, job_id, action, *, reason='', expected_attempt_id=None,
           expected_revision=None, expected_phase=None, expected_authority=None):
    """Asynchronous acceptance is distinct from the worker's verified completion."""
    if action not in {'suspend', 'continue'}:
        raise JobsError('Action must be suspend or continue')
    reason = validate_note(reason)
    preview = readiness(store, job_id, action)
    with store.transaction(create=False) as con:
        job = store.get(con, job_id)
        value = job.get('suspension') or {}
        for actual, expected, label in ((job['attempt_id'], expected_attempt_id, 'attempt'),
                (value.get('revision', 0), expected_revision, 'revision'),
                (value.get('phase', 'Run'), expected_phase, 'phase'),
                (_authority(job), expected_authority, 'authority')):
            if expected is not None and actual != expected:
                raise JobsError(f'Stale suspension {label}; open a fresh confirmation')
        if job['status'] != 'Run' or job.get('cancel_requested') or job.get('termination_requested'):
            raise JobsError('Termination or terminal evidence takes precedence')
        if not _alive_authority(store, job):
            raise JobsError('Execution authority unavailable; no request accepted')
        previous = value.get('request') or {}
        duplicate = previous.get('action') == action and value.get('phase') in (
            {'Suspending', 'Suspended'} if action == 'suspend' else {'Resuming', 'Run'})
        if duplicate:
            blocking = [item for item in preview['reasons'] if not item.startswith(action.capitalize()+' requires verified ')]
            if blocking:
                raise JobsError('; '.join(blocking))
            return {**preview, 'result': 'COMPLETED' if value['phase'] in {'Suspended', 'Run'} else 'PENDING'}
        if not preview['ready'] or value.get('revision', 0) != preview['revision'] or _authority(job) != preview['authority']:
            raise JobsError('; '.join(preview['reasons']) or 'State changed; inspect readiness again')
        revision = value['revision']+1
        request = dict(id=uuid4().hex, action=action, revision=revision, reason=reason,
                       expected_phase=value['phase'], attempt_id=job['attempt_id'],
                       authority=_authority(job), state_identity=_file_identity(store.path), requested_at=time.time())
        value.update(revision=revision, request=request, desired='Suspended' if action == 'suspend' else 'Run',
                     phase='Suspending' if action == 'suspend' else 'Resuming', inhibited=True, reason=reason)
        job['suspension'] = value
        store.save(con, job)
        store.event(con, job['id'], f'{action.capitalize()} request accepted at revision {revision}')
    return {**preview, 'revision': revision, 'phase': value['phase'], 'result': 'ACCEPTED'}


class Channel:
    """Worker side of the private, inode-pinned attempt handshake."""
    def __init__(self, directory, context):
        self.directory, self.context = Path(directory), context

    def _check(self):
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or _file_identity(self.directory) != self.context['directory_identity']):
            raise JobsError('Private suspension channel identity changed')

    def _open_directory(self):
        self._check()
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if {'device': info.st_dev, 'inode': info.st_ino} != self.context['directory_identity']:
            os.close(fd)
            raise JobsError('Suspension channel redirected')
        return fd

    def read(self, name):
        directory_fd = self._open_directory()
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 131072:
                    raise JobsError('Unsafe suspension acknowledgement')
                value = json.load(stream)
            self._check()
            if not isinstance(value, dict):
                raise JobsError('Malformed runner acknowledgement')
            for key in BINDING:
                if value.get(key) != self.context.get(key):
                    raise JobsError(f'Foreign or stale runner acknowledgement: {key}')
            return value
        finally:
            os.close(directory_fd)

    def write(self, name, value):
        directory_fd = self._open_directory()
        temporary = '.'+name+'.'+secrets.token_hex(8)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
            self._check()
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)


@dataclass(frozen=True)
class Domain:
    computational: tuple
    protected: tuple
    launchers: tuple

    def evidence(self):
        return dict(computational=list(self.computational), protected=list(self.protected),
                    launchers=list(self.launchers))


def _descends(pid, ancestor):
    try:
        return pid == ancestor or any(p.pid == ancestor for p in psutil.Process(pid).parents())
    except psutil.Error as exc:
        raise JobsError('Process ancestry cannot be observed') from exc


def _observed_exit(pid, expected=None, anchor=None, *, allow_zombie_topology_loss=False):
    """Distinguish absence or an owned matching zombie from lost visibility."""
    try:
        current = identity(pid)
        if expected is None or current != expected:
            return False
        try:
            if os.getsid(pid) != anchor['pid']:
                return False
        except ProcessLookupError:
            if not allow_zombie_topology_loss:
                raise
            # A previously verified Run member can retain its original zombie
            # identity after macOS drops its SID. This permits pending, not drain.
        return (psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
                and identity(pid) == expected)
    except psutil.ZombieProcess:
        return False  # An unreadable birth identity is not verified absence.
    except (psutil.NoSuchProcess, ProcessLookupError):
        try:
            identity(pid)
        except psutil.ZombieProcess:
            return False
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, OSError):
            return False
        return False
    except (psutil.Error, OSError):
        return False


def _check_identity(member, anchor, *, allow_exit=False):
    if not owner_alive(member):
        if allow_exit and _observed_exit(
                member['pid'], member, anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational member exited during domain observation')
        raise JobsError('Computational identity lost or escaped its owned session')
    try:
        session = os.getsid(member['pid'])
    except ProcessLookupError as exc:
        if allow_exit and _observed_exit(
                member['pid'], member, anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational member exited during domain observation') from exc
        raise
    if session != anchor['pid']:
        raise JobsError('Computational identity lost or escaped its owned session')


def _executable_matches(pid, records):
    try:
        path = Path(psutil.Process(pid).exe()).resolve(strict=True)
        actual = _file_identity(path)
        return any(Path(r['path']).resolve() == path and all(actual[k] == r[k] for k in actual) for r in records)
    except (OSError, psutil.Error, KeyError) as exc:
        raise JobsError('Runtime executable cannot be verified') from exc


def bind_domain(registration, anchor):
    """Classify only descendants of an enrolled runner in the pinned session."""
    if not owner_alive(anchor) or os.getsid(anchor['pid']) != anchor['pid']:
        raise JobsError('Protected ownership anchor is unavailable')
    runner, root = registration['runner'], registration['root']
    _check_identity(runner, anchor); _check_identity(root, anchor, allow_exit=True)
    if not _descends(runner['pid'], anchor['pid']) or not _descends(root['pid'], runner['pid']):
        raise JobsError('Runtime handshake is not bound to the owned payload ancestry')
    if os.getpgid(root['pid']) == anchor['pid']:
        raise JobsError('Computation shares the protected ownership group')
    strategy = registration.get('strategy')
    if strategy not in {'direct-tree', 'openmpi', 'orca-tree'}:
        raise JobsError('Unsupported computational topology')
    runtime = registration.get('runtime') or {}
    if runtime.get('local_only') is not True:
        raise JobsError('Local-only topology was not established')
    launcher_record = runtime.get('mpi_launcher')
    launcher_records = runtime.get('launcher_executables') or ([launcher_record] if launcher_record else [])
    control_records = runtime.get('control_executables', [])
    if launcher_record and not all(Path(r['path']).is_absolute() for r in [launcher_record, *control_records]):
        raise JobsError('MPI runtime executable identities must be absolute')
    for record in [*launcher_records, *control_records, *([runtime['driver']] if runtime.get('driver') else [])]:
        if _file_identity(record['path']) != {key: record[key] for key in ('device', 'inode')}:
            raise JobsError('Registered runtime executable was replaced')
    try:
        descendants = psutil.Process(root['pid']).children(recursive=True)
        members = [root, *(identity(p.pid) for p in descendants if p.status() != psutil.STATUS_ZOMBIE)]
    except psutil.ZombieProcess:
        raise
    except psutil.NoSuchProcess as exc:
        if _observed_exit(exc.pid):
            raise DomainPending('Computational member exited during domain enumeration') from exc
        raise
    protected = [anchor, runner, *registration.get('protected', [])]
    for member in protected:
        _check_identity(member, anchor)
        if not _descends(member['pid'], anchor['pid']):
            raise JobsError('Protected control is outside the owned payload ancestry')
    computational, launchers = [], []
    protected_ids = {p['pid'] for p in protected}
    for member in members:
        pid = member['pid']
        if pid in protected_ids:
            raise JobsError('Computational subtree overlaps protected runner controls')
        _check_identity(member, anchor, allow_exit=True)
        is_launcher = launcher_record and _executable_matches(pid, launcher_records)
        if is_launcher:
            launchers.append(member); protected.append(member)
        elif control_records and _executable_matches(pid, control_records):
            protected.append(member)
        else:
            computational.append(member)
    if strategy == 'openmpi' and (not launchers or launchers[0]['pid'] != root['pid']):
        raise JobsError('Registered MPI launcher identity differs')
    if len(launchers) > 1:
        raise JobsError('Nested or concurrent MPI launchers are unsupported')
    if not computational and not launchers:
        raise DomainPending('No live computational members have registered yet')
    covered = {p['pid'] for p in members+protected}
    # Shell wrappers on the anchor-to-runner path remain protected. Every other
    # session member must belong to the registered computational subtree.
    for pid in group_members(anchor['pid'], session=True):
        if pid not in covered:
            # A producer can fork between the tree and session observations.
            # Classify the new descendant now; the bounded stopped-domain loop
            # must subsequently observe an unchanged identity set twice.
            if _descends(pid, root['pid']):
                member = identity(pid)
                _check_identity(member, anchor, allow_exit=True)
                if launcher_record and _executable_matches(pid, launcher_records):
                    launchers.append(member); protected.append(member)
                elif control_records and _executable_matches(pid, control_records):
                    protected.append(member)
                else:
                    computational.append(member)
            elif _descends(runner['pid'], pid):
                protected.append(identity(pid))
            else:
                raise JobsError('Unregistered member in the owned session')
    if len(launchers) > 1:
        raise JobsError('Nested or concurrent MPI launchers are unsupported')
    for launcher in launchers:
        ranks = [member for member in computational if _descends(member['pid'], launcher['pid'])]
        if len(ranks) < runtime.get('ranks', 1):
            raise DomainPending('MPI ranks have not formed a complete observable domain')
        if len(ranks) != runtime.get('ranks', 1):
            raise JobsError('Dynamic MPI computation differs from the exact registered rank count')
    return Domain(tuple(computational), tuple(protected), tuple(launchers))


def _send(member, anchor, signum):
    _check_identity(member, anchor)
    os.kill(member['pid'], signum)


def _stopped(member, *, exit_anchor=None):
    if not owner_alive(member):
        if exit_anchor is not None and _observed_exit(
                member['pid'], member, exit_anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational member exited during stop-state observation')
        raise JobsError('Computational member disappeared')
    try:
        return psutil.Process(member['pid']).status() == psutil.STATUS_STOPPED
    except psutil.Error as exc:
        if (isinstance(exc, psutil.NoSuchProcess) and exit_anchor is not None
                and _observed_exit(member['pid'], member, exit_anchor, allow_zombie_topology_loss=True)):
            raise DomainPending('Computational member exited during stop-state observation') from exc
        raise JobsError('Computational stop state cannot be observed') from exc


class Coordinator:
    """One instance owned by the existing worker; no adoption or background daemon."""
    def __init__(self, store, job, anchor):
        from .suspension_protocol import clock_descriptor, now_clock
        self.store, self.job_id, self.claim, self.anchor = store, job['id'], job['claim'], anchor
        self.directory = store.root/'attempts'/job['attempt_id']/'suspension'
        self.directory.mkdir(mode=0o700)
        self.context = dict(protocol=PROTOCOL, backend=job['suspension']['backend'], job_id=job['id'],
            attempt_id=job['attempt_id'], claim=job['claim'], worker=job['worker'], anchor=anchor,
            state_identity=_file_identity(store.path), nonce=secrets.token_hex(32),
            directory_identity=_file_identity(self.directory), clock=clock_descriptor())
        self.channel = Channel(self.directory, self.context)
        self.channel.write('context.json', self.context)
        self.registration = None
        self.registered_identity = None
        self.last_check = 0.0
        self.started = now_clock()
        self.pending = None
        self.registration_started = time.monotonic()
        self.domain_pending_since = None
        self.completion_pending_since = None
        self.deadline_termination = False
        self.last_domain = None
        self.fallback_signalled = set()
        self.observed = {}
        self.observed_groups = set()
        self.cancellation_started = None
        self.cancellation_prepared = False
        self.cancellation_escalated = False
        self.denied_signals = []

    @property
    def context_path(self):
        return str(self.directory/'context.json')

    def current(self):
        with self.store.transaction(create=False) as con:
            job = self.store.get(con, self.job_id)
        if job['claim'] != self.claim or job['worker'] != self.context['worker']:
            raise JobsError('Suspension authority changed')
        if _file_identity(self.store.path) != self.context['state_identity']:
            raise JobsError('Jobs state identity changed')
        return job

    def update(self, job, **changes):
        with self.store.transaction(create=False) as con:
            latest = self.store.get(con, self.job_id)
            if latest['claim'] != self.claim or latest['worker'] != self.context['worker']:
                raise JobsError('Suspension authority changed before acknowledgement')
            if latest['suspension']['revision'] != job['suspension']['revision']:
                raise RequestSuperseded('Suspension request changed before acknowledgement')
            if latest['status'] not in ACTIVE:
                return
            latest['suspension'].update(changes)
            self.store.save(con, latest)

    def command(self, job, operation, boundary=None):
        value = job['suspension']
        request = value.get('request') or {}
        self.channel.write('command.json', {**self.context, 'revision': value['revision'],
            'request_id': request.get('id'), 'operation': operation, 'boundary': boundary})

    def ack(self, job, operation, status):
        value = self.channel.read('ack.json')
        if value is None:
            return None
        if value.get('runner') != self.registration['runner']:
            raise JobsError('Runner acknowledgement identity differs')
        request = job['suspension'].get('request') or {}
        if value.get('revision') != job['suspension']['revision'] or value.get('request_id') != request.get('id'):
            return None
        if value.get('operation') != operation:
            return None
        if value.get('status') != status:
            raise JobsError('Runner refused deadline/control preparation: '+str(value.get('error', value.get('status'))))
        return value

    def _registration(self, job):
        record = self.channel.read('registration.json')
        if record is None:
            return False
        if self.registration is not None and record != self.registration:
            raise JobsError('Duplicate or changed runner registration')
        if not lock_held(self.directory/'runner.lock'):
            raise JobsError('Runner control lock is not owned')
        if not record.get('deadline', {}).get('known'):
            raise JobsError('Runner has not declared every timeout owner')
        _storage(record.get('storage'))
        domain = bind_domain(record, self.anchor)
        self.registration = record
        self.last_domain = domain
        self.observe_session()
        from .suspension_protocol import clock_descriptor
        if not job['suspension'].get('capability'):
            self.update(job, clock=clock_descriptor(), clock_started=record.get('clock_started'), capability={
                **{key: record[key] for key in ('protocol', 'backend', 'runner', 'strategy', 'deadline', 'storage', 'runtime')},
                'state_identity': self.context['state_identity'],
                'computational_scope': list(domain.computational), 'protected_scope': list(domain.protected),
                'qualification': {'engine': False, 'sleep_wake': False,
                    'native_mpi': 'runtime-specific synthetic acceptance; see qualification matrix'}})
        return True

    def _evidence(self, domain, *, allow_exit=False):
        from .suspension_protocol import now_clock
        options = {'exit_anchor': self.anchor} if allow_exit else {}
        stopped = []
        for member in domain.computational:
            if _stopped(member, **options):
                if allow_exit:
                    raise JobsError('Unexpected stopped computation outside a confirmed request')
                stopped.append(member)
        return {**domain.evidence(), 'checked_clock': now_clock(), 'observed_at': time.time(),
                'stopped': stopped}

    def _validate(self):
        from .suspension_protocol import clock_descriptor
        if clock_descriptor() != self.context['clock']:
            raise JobsError('Clock/boot discontinuity; retained state is unverified')
        _storage(self.registration['storage'])
        if not owner_alive(self.registration['runner']) or _stopped(self.registration['runner']):
            raise JobsError('Runnable runner authority lost')
        domain = bind_domain(self.registration, self.anchor)
        self.last_domain = domain
        self.observe_session()
        return domain

    def _cancel_pending(self):
        job = self.current()
        return job.get('cancel_requested') or job.get('termination_requested')

    def _suspend(self, job):
        from .suspension_protocol import now_clock
        previous = None
        stable = 0
        limit = time.monotonic()+TRANSITION_TIMEOUT
        signalled = []
        def deliver(member, signum):
            _send(member, self.anchor, signum)
            signalled.append({'identity': member, 'signal': int(signum), 'delivered': True})
            current = self.current()
            previous_evidence = dict(current['suspension'].get('evidence') or {})
            previous_evidence['signals'] = list(signalled)
            self.update(job, evidence=previous_evidence, signal_history=[*current['suspension'].get('signal_history', []), signalled[-1]])
        try:
            while time.monotonic() < limit:
                if self._cancel_pending():
                    return None
                try:
                    domain = self._validate()
                except DomainPending:
                    time.sleep(0.05)
                    continue
                # Freeze the serial producer first. MPI controls receive TSTP
                # and translate to STOP for ranks; they are never STOP targets.
                root = self.registration['root']
                if root in domain.computational and not _stopped(root):
                    deliver(root, signal.SIGSTOP)
                for launcher in domain.launchers:
                    deliver(launcher, signal.SIGTSTP)
                for member in domain.computational:
                    if not any(_descends(member['pid'], launcher['pid']) for launcher in domain.launchers) and not _stopped(member):
                        deliver(member, signal.SIGSTOP)
                    elif domain.launchers and self.registration['strategy'] == 'orca-tree' and member == root:
                        pass
                time.sleep(0.05)
                check = self._validate()
                signature = sorted((m['pid'], m['birth']) for m in check.computational)
                stopped = all(_stopped(m) for m in check.computational)
                stable = stable+1 if stopped and signature == previous else 0
                previous = signature
                self.update(job, evidence={**self._evidence(check), 'signals': signalled})
                if stable >= 2:
                    return check, now_clock()
            raise JobsError('Computational stop verification timed out; partial domain retained without automatic continuation')
        except Exception:
            # Already delivered STOPs remain in evidence; never roll them back.
            raise

    def prepare_cancel(self, job):
        """Inhibit runner producers before TERM and termination-only CONT."""
        if self.registration is None:
            return True
        with self.store.transaction(create=False) as con:
            latest = self.store.get(con, self.job_id)
            if (latest['claim'] != self.claim or latest['worker'] != self.context['worker']
                    or not (latest.get('cancel_requested') or latest.get('termination_requested'))):
                raise JobsError('Termination authority changed')
            state = latest['suspension']
            if (state.get('request') or {}).get('action') != 'cancel':
                state['revision'] += 1
                state['request'] = dict(id=uuid4().hex, action='cancel', revision=state['revision'],
                    attempt_id=latest['attempt_id'], authority=_authority(latest),
                    state_identity=self.context['state_identity'], requested_at=time.time())
                state.update(desired='Terminated', inhibited=True)
                self.store.save(con, latest)
        job = self.current()
        self.command(job, 'cancel')
        limit = time.monotonic()+2
        while time.monotonic() < limit:
            self.current()
            if (self.directory.parent/'payload-exit.json').exists() and self.ready_to_release():
                return False  # Receipt won the request/ACK race; release normally.
            acknowledged = self.ack(job, 'cancel', 'cancel_prepared')
            if acknowledged:
                self.update(job, intervals=acknowledged.get('intervals', []),
                            suspended_seconds=acknowledged.get('suspended_seconds'),
                            unsuspended_elapsed=acknowledged.get('unsuspended_elapsed'))
                return True
            time.sleep(0.05)
        raise JobsError('Runner termination preparation unacknowledged; refusing ambiguous continuation')

    def observe_session(self):
        """Retain original identities across launcher exit and reparenting."""
        self.current()
        snapshot = snapshot_owned_session(self.anchor, self.observed.values())
        previous = set(self.observed)
        rows = {row['identity']['pid']: row for row in snapshot['members']}
        for pid, row in rows.items():
            if self.cancellation_prepared and pid not in previous and row['pgid'] != self.anchor['pid']:
                # Late descendants must still join through an observed owner;
                # an unknown orphan in the SID is insufficient signal authority.
                parent, seen = row['ppid'], {pid}
                while parent not in previous and parent in rows and parent not in seen:
                    seen.add(parent)
                    parent = rows[parent]['ppid']
                if parent not in previous or parent == self.anchor['pid']:
                    raise JobsError('Unregistered late computational member during cancellation')
            self.observed[pid] = row['identity']
            self.observed_groups.add(row['pgid'])
        for item in snapshot['pending']:
            self.observed[item['identity']['pid']] = item['identity']
        return snapshot

    def _groups_absent(self):
        for pgid in self.observed_groups - {self.anchor['pid']}:
            try:
                if group_exists(pgid):
                    return False
            except JobsError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
                return False  # Observation only; denial never means absence.
        return True

    def ready_to_release(self):
        if self.registration is None or self.last_domain is None:
            raise JobsError('Payload receipt lacks a previously verified coordinated domain')
        snapshot = self.observe_session()
        return (not snapshot['pending']
                and {row['identity']['pid'] for row in snapshot['members']} == {self.anchor['pid']}
                and self._groups_absent())

    def _cancel_send(self, member, signum):
        snapshot = self.observe_session()
        rows = {row['identity']['pid']: row for row in snapshot['members']}
        row = rows.get(member['pid'])
        if member in snapshot['absent']:
            return True
        if any(item['identity'] == member for item in snapshot['pending']):
            return False  # No syscall; observe the original until fresh or absent.
        if row is None or row['identity'] != member:
            raise JobsError('Termination target cannot be freshly verified')
        if row['pgid'] == self.anchor['pid'] or member == self.registration['runner']:
            raise JobsError('Protected control cannot be a termination target')
        if row['status'] in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return False  # Observe original reaping; never signal an exiting witness.
        self.current()
        event = {'identity': member, 'signal': int(signum), 'delivered': False,
                 'operation': 'identity-verification'}
        try:
            # Individual identities avoid signalling co-grouped runner controls.
            if identity(member['pid']) != member or os.getsid(member['pid']) != self.anchor['pid'] or os.getpgid(member['pid']) != row['pgid']:
                raise JobsError('Termination target changed before signal delivery')
            event['operation'] = 'signal-delivery'
            os.kill(member['pid'], signum)
            event['delivered'] = True
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess) as exc:
            event['errno'] = getattr(exc, 'errno', None)
            # No retry of the denied signal. Only independent original absence
            # AND kernel group absence can discharge this obligation.
            self.denied_signals.append((member, row['pgid'], time.monotonic(), exc))
        current = self.current()
        self.update(current, signal_history=[*current['suspension'].get('signal_history', []), event])
        return True

    def cancel_step(self, job, grace):
        """One primary initiator, then one bounded handoff to surviving members."""
        self.current()
        now = time.monotonic()
        if self.cancellation_started is None:
            self.cancellation_started = now
        if not self.cancellation_prepared:
            if self.registration is None:
                try:
                    if not self._registration(job):
                        if now-self.cancellation_started >= TRANSITION_TIMEOUT:
                            raise JobsError('Termination requires the coordinated runner handshake')
                        return
                except DomainPending:
                    if now-self.cancellation_started >= TRANSITION_TIMEOUT:
                        raise JobsError('Termination domain did not become verifiable')
                    return
            snapshot = self.observe_session()
            root = self.registration['root']
            root_exiting = (root in snapshot['absent'] or any(
                item['identity'] == root for item in snapshot['pending']) or any(
                row['identity'] == root and row['status'] in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
                for row in snapshot['members']))
            domain = self.last_domain if root_exiting else self._validate()
            if domain is None:
                raise JobsError('No verified computational domain precedes root exit')
            prepared = self.prepare_cancel(job)
            self.current()
            self.cancellation_prepared = True
            self.cancellation_started = time.monotonic()
            if not prepared:
                return
            # MPI controls receive TERM once and own initial rank shutdown.
            # A stopped serial producer requires termination-only CONT after TERM.
            root = self.registration['root']
            for launcher in domain.launchers:
                self._cancel_send(launcher, signal.SIGTERM)
                if self.denied_signals:
                    return
            if root not in domain.launchers:
                delivered = self._cancel_send(root, signal.SIGTERM)
                if delivered and not self.denied_signals:
                    self._cancel_send(root, signal.SIGCONT)
            return
        snapshot = self.observe_session()
        for member, pgid, started, error in list(self.denied_signals):
            absent = member in snapshot['absent']
            try:
                gone = absent and not group_exists(pgid)
            except JobsError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
                gone = False
            if gone:
                self.denied_signals.remove((member, pgid, started, error))
            elif time.monotonic()-started >= .5:
                raise JobsError('Denied termination signal lacks verified original and group absence') from error
        if self.denied_signals:
            return
        if now-self.cancellation_started > grace:
            # Explicit fallback after the primary grace, never a second TERM
            # traversal racing MPI's initial shutdown. Preserve the receipt chain.
            protected = [self.registration['runner'], *self.registration.get('protected', [])]
            for row in snapshot['members']:
                member = row['identity']
                key = (member['pid'], member['birth'])
                if (row['pgid'] != self.anchor['pid'] and member not in protected
                        and key not in self.fallback_signalled):
                    if self._cancel_send(member, signal.SIGKILL):
                        self.fallback_signalled.add(key)
                    if self.denied_signals:
                        break
            self.cancellation_escalated = True
        if now-self.cancellation_started > grace+TRANSITION_TIMEOUT:
            raise JobsError('Termination lacks complete owned drainage and a genuine payload receipt')

    def verify_released(self):
        """After normal RELEASE/reap, require all retained originals and groups gone."""
        limit = time.monotonic()+5
        while True:
            self.current()
            pending = False
            for member in self.observed.values():
                try:
                    current = identity(member['pid'])
                except psutil.ZombieProcess:
                    pending = True
                except psutil.NoSuchProcess:
                    continue
                except ProcessLookupError:
                    pending = True
                else:
                    if current != member:
                        raise JobsError('Released process identity was reused before final verification')
                    pending = True
            try:
                pending = any(group_exists(group) for group in self.observed_groups) or pending
            except JobsError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
                pending = True
            if not pending:
                return
            if time.monotonic() >= limit:
                raise JobsError('Protected control release remains unverified')
            time.sleep(.05)

    def tick(self, job):
        try:
            self._tick(job)
        except RequestSuperseded:
            # A client may commit intent during a routine observation or error
            # report. Preserve that intent and re-read it on the next pass.
            return

    def _tick(self, job):
        from .suspension_protocol import now_clock
        now = now_clock()
        phase = job['suspension']['phase']
        try:
            if self.registration is None:
                if not self._registration(job):
                    if now-self.started > TRANSITION_TIMEOUT:
                        self.update(job, reason='Runner handshake unavailable; suspension unsupported for this attempt')
                    return
                job = self.current()
            expired = self.channel.read('deadline_expired.json')
            if expired is not None:
                if expired.get('runner') != self.registration['runner']:
                    raise JobsError('Foreign deadline owner')
                with self.store.transaction(create=False) as con:
                    latest = self.store.get(con, self.job_id)
                    if latest['claim'] == self.claim and latest['status'] == 'Run':
                        latest.update(status='Cancelling', termination_requested=expired,
                                      reason='Declared execution deadline expired')
                        latest['suspension']['inhibited'] = True
                        self.store.save(con, latest)
                return
            if phase in {'Run', 'Suspending', 'Resuming'} and not owner_alive(self.registration['root']):
                # A genuine root exit can precede the shell receipt. Keep the
                # active commitment while the coordinated runner drains modules
                # and finalizes; an exit race is never a successful suspension.
                self.completion_pending_since = self.completion_pending_since or time.monotonic()
                if time.monotonic()-self.completion_pending_since < TRANSITION_TIMEOUT:
                    self.update(job, reason='Awaiting genuine runner completion and owned-session drainage',
                                inhibited=True, evidence=None, verified_at=None)
                    return
                raise JobsError('Computational root exited without a completed runner receipt')
            if phase == 'Unverified':
                request = job['suspension'].get('request') or {}
                if request.get('id'):
                    invalidated = self.ack(job, 'invalidate', 'invalidated')
                    if invalidated:
                        self.update(job, intervals=invalidated.get('intervals', []),
                                    suspended_seconds=invalidated.get('suspended_seconds'), unsuspended_elapsed=None)
                return
            if phase == 'Run':
                if now-self.last_check < VERIFY_CADENCE:
                    return
                domain = self._validate()
                # A Run member may finish after domain validation; only verified
                # exit enters the existing bounded wait for genuine completion.
                evidence = self._evidence(domain, allow_exit=True)
                self.last_check = now
                self.domain_pending_since = None
                self.update(job, evidence=evidence, verified_at=time.time())
                return
            if phase == 'Suspended':
                if now-self.last_check < VERIFY_CADENCE:
                    return
                self.last_check = now
                domain = self._validate()
                old = job['suspension']['evidence']['computational']
                if set((p['pid'],p['birth']) for p in old) != set((p['pid'],p['birth']) for p in domain.computational) or not all(_stopped(m) for m in domain.computational):
                    raise JobsError('Suspended membership lost, changed, or externally continued')
                ack = self.ack(job, 'suspended', 'paused')
                # The runner can refresh its acknowledgement during validation.
                checked_at = now_clock()
                if not ack or not isinstance(ack.get('observed_clock'), (int, float)) or not 0 <= checked_at-ack['observed_clock'] <= 3:
                    raise JobsError('Fresh suspended runner/deadline acknowledgement unavailable')
                self.update(job, evidence=self._evidence(domain), verified_at=time.time(),
                            intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                            unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                return
            request_id = job['suspension']['request']['id']
            if self.pending is None or self.pending['id'] != request_id:
                operation = 'prepare_suspend' if phase == 'Suspending' else 'prepare_continue'
                boundary = now if phase == 'Resuming' else None
                self.pending = {'id':request_id,'stage':operation,'started':time.monotonic(), 'boundary':boundary}
                self.command(job, operation, boundary)
                return
            if time.monotonic()-self.pending['started'] > TRANSITION_TIMEOUT+2:
                raise JobsError('Runner transition acknowledgement timed out; partial state retained')
            stage = self.pending['stage']
            if stage == 'prepare_suspend' and self.ack(job, stage, 'prepared'):
                result = self._suspend(job)
                if result is None:
                    return
                domain, boundary = result
                self.update(job, evidence=self._evidence(domain))
                self.command(job, 'suspended', boundary)
                self.pending.update(stage='suspended', boundary=boundary)
            elif stage == 'suspended':
                ack = self.ack(job, stage, 'paused')
                if ack:
                    domain = self._validate()
                    if not all(_stopped(m) for m in domain.computational):
                        raise JobsError('Stop state changed before suspension acknowledgement')
                    self.update(job, phase='Suspended', evidence=self._evidence(domain), verified_at=time.time(),
                                intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                                unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                    self.pending = None
            elif stage == 'prepare_continue' and (prepared := self.ack(job, stage, 'resume_prepared')):
                self.update(job, intervals=prepared.get('intervals', []), suspended_seconds=prepared.get('suspended_seconds'),
                            unsuspended_elapsed=prepared.get('unsuspended_elapsed'))
                domain = self._validate()
                old = job['suspension']['evidence']['computational']
                if sorted((p['pid'],p['birth']) for p in old) != sorted((p['pid'],p['birth']) for p in domain.computational) or not all(_stopped(m) for m in domain.computational):
                    raise JobsError('Continue requires the same stopped computational identities')
                if self._cancel_pending():
                    return
                # Release MPI ranks before serial driver, preserving the held
                # runner until runnable-state verification has completed.
                for launcher in domain.launchers:
                    _send(launcher, self.anchor, signal.SIGCONT)
                for member in reversed(domain.computational):
                    _send(member, self.anchor, signal.SIGCONT)
                self.pending.update(stage='verify_continue', domain=domain)
            elif stage == 'verify_continue':
                domain = self._validate()
                prior = self.pending['domain'].computational
                if sorted((p['pid'],p['birth']) for p in prior) != sorted((p['pid'],p['birth']) for p in domain.computational):
                    raise JobsError('Computational identities changed during Continue verification')
                if any(_stopped(m) for m in domain.computational):
                    return
                self.command(job, 'run')
                self.pending.update(stage='run')
            elif stage == 'run':
                ack = self.ack(job, stage, 'running')
                if ack:
                    domain = self._validate()
                    prior = self.pending['domain'].computational
                    if sorted((p['pid'],p['birth']) for p in prior) != sorted((p['pid'],p['birth']) for p in domain.computational) or any(_stopped(m) for m in domain.computational):
                        raise JobsError('Continue acknowledgement lost original computational domain')
                    self.update(job, phase='Run', inhibited=False, evidence=self._evidence(domain), verified_at=time.time(),
                                intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                                unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                    self.pending = None
        except RequestSuperseded:
            raise
        except (JobsError, OSError, ValueError, KeyError, psutil.Error) as exc:
            if isinstance(exc, DomainPending) and phase == 'Run':
                self.domain_pending_since = self.domain_pending_since or time.monotonic()
                if time.monotonic()-self.domain_pending_since < TRANSITION_TIMEOUT:
                    self.update(job, reason=str(exc), evidence=None, verified_at=None)
                    return
            if self.registration is None:
                # Launch-time enumeration can race module/rank startup. No
                # signal is permitted until a complete handshake is observed.
                self.update(job, reason='Runner enrollment unverified: '+str(exc), verified_at=None)
                return
            self.update(job, phase='Unverified', inhibited=True, reason=str(exc), verified_at=None,
                        unsuspended_elapsed=None, accounting_uncertain=True)
            request = job['suspension'].get('request') or {}
            if request.get('id'):
                boundary = (job['suspension'].get('evidence') or {}).get('checked_clock', now)
                try:
                    self.command(job, 'invalidate', boundary)
                except (JobsError, OSError):
                    # Broken IPC cannot authorize rollback or continued accounting.
                    pass
