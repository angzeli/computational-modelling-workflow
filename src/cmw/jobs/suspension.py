"""Opt-in execution control. Clients persist intent; only the owning worker signals.

Canonical Run remains active throughout a suspension. Schema 3 prevents older
workers from mistaking the orthogonal metadata for a dispatch opportunity.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import time
from uuid import uuid4

import psutil

from .ownership import (identity_for, owner_alive, observe_owner, OwnershipStatus, OwnershipUnavailable,
                        OwnershipContradiction, ObservationFinished, is_unavailable,
                        lock_held, group_members, snapshot_owned_session, group_exists)
from .store import ACTIVE, JobsError, validate_note
from .ownership_hold import blocks_replacement, retry_read
from .process_identity import ConfirmedProcessAbsence, ancestor_pids, descendant_pids

BACKENDS = ('local-command-v1', 'vasp-port-v1', 'orca-local-v1')
PROTOCOL = 1
TRANSITION_TIMEOUT = 6.0
VERIFY_CADENCE = 1.0

class DomainPending(ObservationFinished):
    """An enrolled computational domain is changing; it is not signal-ready."""


class CompletionDomainUnobserved(ObservationFinished):
    """A receipt cannot reconstruct a live process-domain census never obtained."""
    deciding_check = 'completion.domain_never_observed'

    def __init__(self):
        super().__init__('process domain never observed; receipt present; manual verification required')


class RequestSuperseded(ObservationFinished):
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
    if (lock_held(store.root/'controller.lock') or blocks_replacement(control.get('owner'))):
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
    return (not job.get('ownership_holds') and job.get('worker_protocol') == 3 and owner_alive(job.get('worker'))
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
        try:
            info = path.stat()
        except OSError as exc:
            raise OwnershipUnavailable('storage', f'Required storage temporarily unreadable: {path}') from exc
        if (not stat.S_ISDIR(info.st_mode)
                or {'device': info.st_dev, 'inode': info.st_ino} != {k: record[k] for k in ('device', 'inode')}):
            raise OwnershipContradiction(f'Required storage was replaced: {path}')


def _present(path):
    """Do not let Python's exists() turn a denied stat into missing evidence."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


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
    completion_pending = 'completion' in job.get('ownership_holds', {})
    if completion_pending:
        reasons.append('Completion pending; controls are refused until established completion evidence is recorded')
    if job['status'] != 'Run' or job.get('cancel_requested') or job.get('termination_requested'):
        reasons.append('Attempt is not running or termination has precedence')
    if not completion_pending and not _alive_authority(store, job):
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
                raise JobsError('Coordinated runner ownership cannot be verified')
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


def finalize_timing(job, *, now=None, descriptor=None):
    """Freeze operational accounting at genuine terminal evidence, not a stop."""
    value = job.get('suspension')
    if not value:
        return
    try:
        from .suspension_protocol import now_clock, clock_descriptor
        end = now_clock() if now is None else now
        start = value.get('clock_started')
        observed_clock = clock_descriptor() if descriptor is None else descriptor
        if value.get('clock') != observed_clock or start is None or end < start or value.get('accounting_uncertain'):
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
        return _submit(store, con, job_id, action, preview, reason=reason,
                       expected_attempt_id=expected_attempt_id, expected_revision=expected_revision,
                       expected_phase=expected_phase, expected_authority=expected_authority)


def _submit(store, con, job_id, action, preview, *, reason='', expected_attempt_id=None,
            expected_revision=None, expected_phase=None, expected_authority=None):
    """Shared intent insertion; only the owning worker acts on this request."""
    job = store.get(con, job_id)
    value = job.get('suspension') or {}
    for actual, expected, label in ((job['attempt_id'], expected_attempt_id, 'attempt'),
            (value.get('revision', 0), expected_revision, 'revision'),
            (value.get('phase', 'Run'), expected_phase, 'phase'),
            (_authority(job), expected_authority, 'authority')):
        if expected is not None and actual != expected:
            raise JobsError(f'Stale suspension {label}; open a fresh confirmation')
    if 'completion' in job.get('ownership_holds', {}):
        raise JobsError('Completion pending; no suspension request accepted')
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
            raise OwnershipContradiction('Private suspension channel identity changed')

    def _open_directory(self):
        self._check()
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if {'device': info.st_dev, 'inode': info.st_ino} != self.context['directory_identity']:
                raise OwnershipContradiction('Suspension channel redirected')
        except BaseException:
            os.close(fd)
            raise
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
                    raise OwnershipContradiction('Unsafe suspension acknowledgement')
                value = json.load(stream)
            self._check()
            if not isinstance(value, dict):
                raise OwnershipContradiction('Malformed runner acknowledgement')
            for key in BINDING:
                if value.get(key) != self.context.get(key):
                    raise OwnershipContradiction(f'Foreign or stale runner acknowledgement: {key}')
            return value
        finally:
            os.close(directory_fd)

    def write(self, name, value, *, publication=None, exclusive=False):
        temporary = '.'+name+'.'+secrets.token_hex(8)
        directory_fd = retry_read(self._open_directory)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
            with publication if publication is not None else nullcontext():
                # A normal delivery gate must be released for re-evaluation;
                # initial context publication has no gate or effect to replay.
                if publication is None:
                    retry_read(self._check)
                else:
                    self._check()
                if exclusive:
                    os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                            follow_symlinks=False)
                else:
                    os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            def discard_temporary():
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            try:
                retry_read(discard_temporary)
            finally:
                os.close(directory_fd)


@dataclass(frozen=True)
class Domain:
    computational: tuple
    protected: tuple
    launchers: tuple

    def evidence(self):
        return dict(computational=list(self.computational), protected=list(self.protected),
                    launchers=list(self.launchers))


def _domain_members(records):
    return {record['pid']: record for record in records}


def _process_read(check, read, *args):
    """Preserve lifecycle races handled by the caller; hold on unreadable kernel evidence."""
    try:
        return read(*args)
    except psutil.ZombieProcess as exc:
        raise OwnershipUnavailable(check, 'Exiting process identity is unreadable') from exc
    except (ProcessLookupError, psutil.NoSuchProcess):
        raise
    except (OSError, psutil.Error) as exc:
        raise OwnershipUnavailable(check, 'Process evidence is temporarily unreadable') from exc


def _descends(pid, ancestor):
    try:
        return pid == ancestor or ancestor in ancestor_pids(pid, ancestor)
    except (OSError, psutil.Error) as exc:
        raise OwnershipUnavailable('ancestry', 'Process ancestry cannot be observed') from exc


def _observed_exit(pid, expected=None, anchor=None, *, allow_zombie_topology_loss=False):
    """Distinguish absence or an owned matching zombie from lost visibility."""
    try:
        current = identity_for(expected or anchor, pid)
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
                and identity_for(expected, pid) == expected)
    except psutil.ZombieProcess:
        return False  # An unreadable birth identity is not verified absence.
    except (psutil.NoSuchProcess, ProcessLookupError):
        try:
            identity_for(expected or anchor, pid)
        except psutil.ZombieProcess:
            return False
        except (ConfirmedProcessAbsence, psutil.NoSuchProcess):
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
        result = observe_owner(member)
        if result.status != OwnershipStatus.GONE:
            raise OwnershipUnavailable(result.deciding_check, 'Computational identity lost or escaped its owned session')
        raise OwnershipContradiction('Computational identity lost or escaped its owned session')
    try:
        session = _process_read('member_session', os.getsid, member['pid'])
    except ProcessLookupError as exc:
        if allow_exit and _observed_exit(
                member['pid'], member, anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational member exited during domain observation') from exc
        raise
    if session != anchor['pid']:
        raise OwnershipContradiction('Computational identity lost or escaped its owned session')


def _executable_matches(pid, records):
    try:
        path = Path(psutil.Process(pid).exe()).resolve(strict=True)
        actual = _file_identity(path)
        return any(Path(r['path']).resolve() == path and all(actual[k] == r[k] for k in actual) for r in records)
    except (OSError, psutil.Error) as exc:
        raise OwnershipUnavailable('executable', 'Runtime executable cannot be observed') from exc


def _check_runner(registration, anchor, *, check_stopped=False):
    runner, root = registration['runner'], registration['root']
    try:
        _check_identity(runner, anchor, allow_exit=True)
        if check_stopped and _stopped(runner, exit_anchor=anchor):
            raise OwnershipContradiction('Runnable runner authority lost')
    except DomainPending as exc:
        # The Run root probe can precede both ordinary exits. This grants only
        # the existing bounded receipt wait, never drainage or signal authority.
        if _observed_exit(root['pid'], root, anchor, allow_zombie_topology_loss=True):
            _check_identity(anchor, anchor)
            if _stopped(anchor):
                raise OwnershipContradiction('Protected ownership anchor is not runnable') from exc
            raise DomainPending('Registered runner and computation exited during observation') from exc
        result = observe_owner(root)
        if result.status == OwnershipStatus.UNVERIFIABLE:
            raise OwnershipUnavailable(result.deciding_check, 'Root unreadable after runner exit') from exc
        raise OwnershipContradiction('Runnable runner authority lost while computation remains unverified') from exc


def bind_domain(registration, anchor):
    """Classify only descendants of an enrolled runner in the pinned session."""
    if not owner_alive(anchor):
        result = observe_owner(anchor)
        if result.status != OwnershipStatus.GONE:
            raise OwnershipUnavailable(result.deciding_check, 'Protected ownership anchor is unavailable')
        raise OwnershipContradiction('Protected ownership anchor is unavailable')
    if _process_read('anchor_session', os.getsid, anchor['pid']) != anchor['pid']:
        raise OwnershipContradiction('Protected ownership anchor is outside its owned session')
    runner, root = registration['runner'], registration['root']
    _check_runner(registration, anchor); _check_identity(root, anchor, allow_exit=True)
    if not _descends(runner['pid'], anchor['pid']) or not _descends(root['pid'], runner['pid']):
        raise OwnershipContradiction('Runtime handshake is not bound to the owned payload ancestry')
    try:
        root_group = _process_read('root_group', os.getpgid, root['pid'])
    except ProcessLookupError as exc:
        if _observed_exit(root['pid'], root, anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational root exited during group observation') from exc
        raise
    if root_group == anchor['pid']:
        raise OwnershipContradiction('Computation shares the protected ownership group')
    strategy = registration.get('strategy')
    if strategy not in {'direct-tree', 'openmpi', 'orca-tree'}:
        raise OwnershipContradiction('Unsupported computational topology')
    runtime = registration.get('runtime') or {}
    if runtime.get('local_only') is not True:
        raise OwnershipContradiction('Local-only topology was not established')
    launcher_record = runtime.get('mpi_launcher')
    launcher_records = runtime.get('launcher_executables') or ([launcher_record] if launcher_record else [])
    control_records = runtime.get('control_executables', [])
    if launcher_record and not all(Path(r['path']).is_absolute() for r in [launcher_record, *control_records]):
        raise OwnershipContradiction('MPI runtime executable identities must be absolute')
    for record in [*launcher_records, *control_records, *([runtime['driver']] if runtime.get('driver') else [])]:
        if _file_identity(record['path']) != {key: record[key] for key in ('device', 'inode')}:
            raise OwnershipContradiction('Registered runtime executable was replaced')
    try:
        members = [root]
        for pid in descendant_pids(root['pid']):
            member = identity_for(anchor, pid)
            if not _descends(pid, root['pid']) or identity_for(anchor, pid) != member:
                raise OwnershipUnavailable('ancestry', 'Computational subtree changed during enumeration')
            members.append(member)
    except (OSError, psutil.Error) as exc:
        if isinstance(exc, psutil.NoSuchProcess) and _observed_exit(exc.pid, anchor=anchor):
            raise DomainPending('Computational member exited during domain enumeration') from exc
        raise OwnershipUnavailable('descendants', 'Computational subtree cannot be observed') from exc
    protected = [anchor, runner, *registration.get('protected', [])]
    for member in protected:
        _check_identity(member, anchor)
        if not _descends(member['pid'], anchor['pid']):
            raise OwnershipContradiction('Protected control is outside the owned payload ancestry')
    computational, launchers = [], []
    protected_ids = {p['pid'] for p in protected}
    for member in members:
        pid = member['pid']
        if pid in protected_ids:
            raise OwnershipContradiction('Computational subtree overlaps protected runner controls')
        _check_identity(member, anchor, allow_exit=True)
        is_launcher = launcher_record and _executable_matches(pid, launcher_records)
        if is_launcher:
            launchers.append(member); protected.append(member)
        elif control_records and _executable_matches(pid, control_records):
            protected.append(member)
        else:
            computational.append(member)
    if strategy == 'openmpi' and (not launchers or launchers[0]['pid'] != root['pid']):
        raise OwnershipContradiction('Registered MPI launcher identity differs')
    if len(launchers) > 1:
        raise OwnershipContradiction('Nested or concurrent MPI launchers are unsupported')
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
                member = _process_read('member_identity', identity_for, anchor, pid)
                _check_identity(member, anchor, allow_exit=True)
                if launcher_record and _executable_matches(pid, launcher_records):
                    launchers.append(member); protected.append(member)
                elif control_records and _executable_matches(pid, control_records):
                    protected.append(member)
                else:
                    computational.append(member)
            elif _descends(runner['pid'], pid):
                protected.append(_process_read('protected_identity', identity_for, anchor, pid))
            else:
                raise OwnershipContradiction('Unregistered member in the owned session')
    if len(launchers) > 1:
        raise OwnershipContradiction('Nested or concurrent MPI launchers are unsupported')
    for launcher in launchers:
        ranks = [member for member in computational if _descends(member['pid'], launcher['pid'])]
        if len(ranks) < runtime.get('ranks', 1):
            raise DomainPending('MPI ranks have not formed a complete observable domain')
        if len(ranks) != runtime.get('ranks', 1):
            raise OwnershipContradiction('Dynamic MPI computation differs from the exact registered rank count')
    return Domain(tuple(computational), tuple(protected), tuple(launchers))


def _signal_witness(member, anchor, expected_pgid=None):
    """One observation under the delivery lock. Never wait or retry in this scope."""
    def require_live(owner):
        result = observe_owner(owner)
        if result.status == OwnershipStatus.UNVERIFIABLE:
            raise OwnershipUnavailable(result.deciding_check, 'Signal authority cannot be verified')
        if result.status != OwnershipStatus.VERIFIED:
            raise OwnershipContradiction('Signal authority changed before delivery')

    require_live(anchor)
    try:
        sid = anchor['pid']
        if os.getsid(sid) != sid or os.getpgid(sid) != sid or os.getsid(member['pid']) != sid:
            raise OwnershipContradiction('Signal target or anchor left the owned session')
        pgid = os.getpgid(member['pid'])
        if pgid == sid or (expected_pgid is not None and pgid != expected_pgid):
            raise OwnershipContradiction('Signal target changed group before delivery')
    except OSError as exc:
        # Missing topology alone cannot prove exit; check each original pin.
        require_live(anchor)
        require_live(member)
        raise OwnershipUnavailable('signal_topology', 'Signal topology cannot be read') from exc
    require_live(member)


def _send(member, anchor, signum, *, authority=None, delivery=None):
    if authority:
        authority.read_domain(lambda: _check_identity(member, anchor))
    else:
        _signal_witness(member, anchor)
    with delivery if delivery is not None else nullcontext():
        os.kill(member['pid'], signum)


def _stopped(member, *, exit_anchor=None):
    if not owner_alive(member):
        if exit_anchor is not None and _observed_exit(
                member['pid'], member, exit_anchor, allow_zombie_topology_loss=True):
            raise DomainPending('Computational member exited during stop-state observation')
        result = observe_owner(member)
        if result.status == OwnershipStatus.UNVERIFIABLE:
            raise OwnershipUnavailable(result.deciding_check, 'Computational member cannot be verified')
        raise OwnershipContradiction('Computational member cannot be verified')
    try:
        return psutil.Process(member['pid']).status() == psutil.STATUS_STOPPED
    except (OSError, psutil.Error) as exc:
        if (isinstance(exc, psutil.NoSuchProcess) and exit_anchor is not None
                and _observed_exit(member['pid'], member, exit_anchor, allow_zombie_topology_loss=True)):
            raise DomainPending('Computational member exited during stop-state observation') from exc
        raise OwnershipUnavailable('stop_state', 'Computational stop state cannot be observed') from exc


class Coordinator:
    """One instance owned by the existing worker; no adoption or background daemon."""
    authority = None

    def __init__(self, store, job, anchor):
        from .suspension_protocol import clock_descriptor, now_clock
        self.store, self.job_id, self.claim, self.anchor = store, job['id'], job['claim'], anchor
        self.directory = store.root/'attempts'/job['attempt_id']/'suspension'
        nonce = secrets.token_hex(32)
        self.directory.mkdir(mode=0o700)
        self.context = dict(protocol=PROTOCOL, backend=job['suspension']['backend'], job_id=job['id'],
            attempt_id=job['attempt_id'], claim=job['claim'], worker=job['worker'], anchor=anchor,
            state_identity=retry_read(lambda: _file_identity(store.path)), nonce=nonce,
            directory_identity=retry_read(lambda: _file_identity(self.directory)), clock=retry_read(clock_descriptor))
        self.channel = Channel(self.directory, self.context)
        self._initialize_context()
        self.registration = None
        self.registered_identity = None
        self.last_check = 0.0
        self.started = retry_read(now_clock)
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
        self.cancel_intent = None
        self.termination_signals = set()
        self.cancellation_escalated = False
        self.denied_signals = []
        self.pending_updates = []
        self.pending_command = None
        self.delivered_signals = set()

    def _initialize_context(self):
        """Resume this instance's publication without recreating its directory.

        Before publication a failed private temporary write can be retried.
        After publication, exact readback resolves the outcome; only directory
        durability is retried. A different context is never overwritten.
        """
        def read_context():
            try:
                return self.channel.read('context.json')
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OwnershipContradiction('Initial suspension context is redirected') from exc
                raise
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise OwnershipContradiction('Malformed initial suspension context') from exc

        def durable():
            descriptor = self.channel._open_directory()
            try:
                os.fsync(descriptor)
                self.channel._check()
            finally:
                os.close(descriptor)

        delay = .25
        while True:
            existing = retry_read(read_context)
            if existing is not None:
                if existing != self.context:
                    raise OwnershipContradiction('Initial suspension context changed; refusing overwrite')
                retry_read(durable)
                return
            try:
                self.channel.write('context.json', self.context, exclusive=True)
                return
            except Exception as exc:
                if not is_unavailable(exc):
                    raise
            time.sleep(delay)
            delay = min(5., delay * 2)

    def discard_before_go(self):
        # Only this worker created the channel and GO has never been sent.
        def existing_directory():
            try:
                return self.directory.lstat()
            except FileNotFoundError:
                return None
        info = retry_read(existing_directory)
        if getattr(self, '_discard_directory_removed', False):
            if info is not None:
                raise OwnershipContradiction('Discarded suspension directory was replaced')
            return
        if info is None:
            raise OwnershipContradiction('Unstarted suspension directory disappeared before removal')
        descriptor = retry_read(self.channel._open_directory)
        try:
            context = retry_read(lambda: self.channel.read('context.json'))
            if getattr(self, '_discard_context_removed', False):
                if context is not None:
                    raise OwnershipContradiction('Discarded suspension context was replaced')
            else:
                if context != self.context:
                    raise OwnershipContradiction('Unstarted suspension channel changed; refusing reuse')
                os.unlink('context.json', dir_fd=descriptor)
                self._discard_context_removed = True
            retry_read(self.channel._check)
            self.directory.rmdir()
            self._discard_directory_removed = True
        finally:
            os.close(descriptor)

    @property
    def context_path(self):
        return str(self.directory/'context.json')

    def _queue_transaction(self, *, delivery=False):
        wait = self.authority._backoff if self.authority else None
        if delivery:
            return self.store.delivery_gate(wait=wait)
        return self.store.transaction(create=False, wait=wait)

    def _current(self):
        with self._queue_transaction() as con:
            job = self.store.get(con, self.job_id)
        if job['claim'] != self.claim or job['worker'] != self.context['worker']:
            raise OwnershipContradiction('Suspension authority changed')
        if _file_identity(self.store.path) != self.context['state_identity']:
            raise OwnershipContradiction('Jobs state identity changed')
        return job

    def current(self, *, completion=False):
        # Completion has its own retry driver after the anchor is released.
        return self._current() if completion else self._read(self._current)

    def _channel_read(self, name):
        return self._read(lambda: self.channel.read(name))

    @contextmanager
    def _intent(self, expected, *, check_deadline=False, delivery=False):
        # Serialize only the final publication/signal with client mutations.
        # All blocking authority and topology retries run outside this transaction.
        while True:
            with ExitStack() as stack:
                try:
                    con = stack.enter_context(self._queue_transaction(delivery=delivery))
                    latest = self.store.get(con, self.job_id)
                    if (latest['claim'] != self.claim or latest['worker'] != self.context['worker']
                            or latest['attempt_id'] != expected['attempt_id'] or latest['group'] != self.anchor
                            or _file_identity(self.store.path) != self.context['state_identity']):
                        raise OwnershipContradiction('Suspension authority changed')
                    fields = ('status', 'cancel_requested', 'termination_requested')
                    if (any(latest.get(key) != expected.get(key) for key in fields)
                            or latest.get('ownership_holds')
                            or any(latest['suspension'].get(key) != expected['suspension'].get(key)
                                   for key in ('revision', 'request'))):
                        raise RequestSuperseded('Control request or termination intent changed')
                    if check_deadline and _present(self.directory/'deadline_expired.json'):
                        raise RequestSuperseded('Runner termination request pending verification')
                except Exception as exc:
                    if not is_unavailable(exc):
                        raise
                    failure = exc
                else:
                    # Neither a body effect nor its COMMIT is retried here.
                    yield con, latest
                    return
            if not self.authority:
                raise OwnershipUnavailable('suspension.intent', 'Control intent temporarily unreadable') from failure
            self.authority.read_domain(self._current, first_failure=failure)

    def _apply_update(self, job, changes):
        with self._intent(job) as (con, latest):
            if latest['status'] in ACTIVE:
                # A readback after an uncertain COMMIT may find this exact
                # update already recorded. Do not repeat the mutation.
                if any(latest['suspension'].get(key) != value for key, value in changes.items()):
                    latest['suspension'].update(changes)
                    self.store.save(con, latest)

    def update(self, job, **changes):
        try:
            self._apply_update(job, changes)
        except Exception as exc:
            if isinstance(exc, RequestSuperseded) or is_unavailable(exc):
                pending = getattr(self, 'pending_updates', [])
                pending.append((deepcopy(job), deepcopy(changes)))
                self.pending_updates = pending
            raise

    @staticmethod
    def _same_intent(expected, current):
        return (all(expected.get(key) == current.get(key) for key in
                    ('attempt_id', 'claim', 'worker', 'group', 'status',
                     'cancel_requested', 'termination_requested'))
                and all(expected['suspension'].get(key) == current['suspension'].get(key)
                        for key in ('revision', 'request')))

    def _flush_updates(self):
        pending = getattr(self, 'pending_updates', [])
        while pending:
            expected, changes = pending[0]
            latest = self.current()
            if not self._same_intent(expected, latest):
                pending.pop(0)  # A newer accepted request wins; do not replay its predecessor.
                continue
            if latest.get('ownership_holds'):
                raise RequestSuperseded('Control update waits for observation recovery')
            if changes.get('evidence') is not None:
                try:
                    domain = self._validate()
                    target = changes.get('phase')
                    stopped = [self._stopped(member) for member in domain.computational]
                    if ((target == 'Suspended' and not all(stopped)) or
                            (target == 'Run' and any(stopped))):
                        pending.pop(0)  # Let the phase driver establish a fresh transition.
                        continue
                    changes = {**changes, 'evidence': self._evidence(
                        domain, allow_exit=latest['suspension']['phase'] == 'Run'),
                        'verified_at': time.time()}
                except DomainPending:
                    # A new exit prevents publishing stale live evidence, but
                    # does not erase an already delivered signal's history.
                    if 'signal_history' in changes:
                        self._apply_update(latest, {'signal_history': changes['signal_history']})
                    pending.pop(0)  # The phase driver will record the newly observed exit.
                    continue
            self._apply_update(latest, changes)
            pending.pop(0)

    def command(self, job, operation, boundary=None):
        if self.authority:
            self.authority.wait()
        if operation != 'cancel' and (job['status'] != 'Run' or job.get('cancel_requested')
                                      or job.get('termination_requested')):
            raise RequestSuperseded('Termination superseded suspension command')
        value = job['suspension']
        request = value.get('request') or {}
        command = {**self.context, 'revision': value['revision'],
                   'request_id': request.get('id'), 'operation': operation, 'boundary': boundary}
        self.pending_command = (deepcopy(job), command)
        self._flush_command()

    def _flush_command(self):
        pending = getattr(self, 'pending_command', None)
        if pending is None:
            return
        expected, command = pending
        latest = self.current()
        if not self._same_intent(expected, latest):
            self.pending_command = None
            raise RequestSuperseded('A newer request superseded the pending command')
        if latest.get('ownership_holds'):
            raise RequestSuperseded('Command waits for observation recovery')
        if self._channel_read('command.json') != command:
            self.channel.write('command.json', command, publication=self._intent(
                latest, check_deadline=command['operation'] not in {'cancel', 'invalidate'}, delivery=True))
        self.pending_command = None

    def ack(self, job, operation, status):
        value = self._channel_read('ack.json')
        if value is None:
            return None
        if value.get('runner') != self.registration['runner']:
            raise OwnershipContradiction('Runner acknowledgement identity differs')
        request = job['suspension'].get('request') or {}
        if value.get('revision') != job['suspension']['revision'] or value.get('request_id') != request.get('id'):
            return None
        if value.get('operation') != operation:
            return None
        if value.get('status') != status:
            raise OwnershipContradiction('Runner refused deadline/control preparation: '+str(value.get('error', value.get('status'))))
        return value

    def _registration(self, job):
        record = self._channel_read('registration.json')
        if record is None:
            return False
        if self.registration is not None and record != self.registration:
            raise OwnershipContradiction('Duplicate or changed runner registration')
        if not self._read(lambda: lock_held(self.directory/'runner.lock', strict=True)):
            # Runner exit can precede its shell's receipt. Confirming that
            # ordering supplies no domain proof; retain the receipt driver.
            self._read(lambda: _check_runner(record, self.anchor))
            raise OwnershipContradiction('Runner control lock is not owned')
        if not record.get('deadline', {}).get('known'):
            raise OwnershipContradiction('Runner has not declared every timeout owner')
        self._read(lambda: _storage(record.get('storage')))
        def read():
            return bind_domain(record, self.anchor)
        domain = self.authority.read_domain(read) if self.authority else read()
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

    def _stopped(self, member, **options):
        return self._read(lambda: _stopped(member, **options))

    def _evidence(self, domain, *, allow_exit=False):
        from .suspension_protocol import now_clock
        options = {'exit_anchor': self.anchor} if allow_exit else {}
        stopped = []
        for member in domain.computational:
            if self._stopped(member, **options):
                if allow_exit:
                    raise OwnershipContradiction('Unexpected stopped computation outside a confirmed request')
                stopped.append(member)
        return {**domain.evidence(), 'checked_clock': now_clock(), 'observed_at': time.time(),
                'stopped': stopped}

    def _validate(self):
        return self._read(self._observe_domain)

    def _observe_domain(self):
        from .suspension_protocol import clock_descriptor
        if clock_descriptor() != self.context['clock']:
            raise OwnershipContradiction('Clock/boot discontinuity; retained state is unverified')
        _storage(self.registration['storage'])
        _check_runner(self.registration, self.anchor, check_stopped=True)
        domain = bind_domain(self.registration, self.anchor)
        self.last_domain = domain
        self.observe_session()
        return domain

    def _read(self, observation):
        return self.authority.read_domain(observation) if self.authority else observation()

    def _transition_time(self):
        # Keep internal ACK deadlines independent of authority-hold backoff.
        # Runner execution budgets and committed suspension intervals are untouched.
        return time.monotonic() - (self.authority.hold_seconds if self.authority else 0.)

    def _root_exited(self):
        root = self.registration['root']
        result = observe_owner(root)
        if result.status == OwnershipStatus.VERIFIED:
            return False
        if result.status == OwnershipStatus.UNVERIFIABLE:
            # A positively identified zombie can precede the real runner receipt.
            # A denied status or birth probe cannot establish that exit.
            if (result.deciding_check == 'process_status'
                    and result.checks['process_status']['observed'] == 'zombie/dead'
                    and _observed_exit(root['pid'], root, self.anchor, allow_zombie_topology_loss=True)):
                return True
            raise OwnershipUnavailable(result.deciding_check, 'Computational root ownership is unreadable')
        if result.deciding_check == 'pid':
            return True
        raise OwnershipContradiction('Computational root identity changed')

    def _cancel_pending(self):
        job = self.current()
        return job.get('cancel_requested') or job.get('termination_requested')

    @contextmanager
    def _signal_gate(self, job, member, *, expected_pgid=None, check_deadline=False):
        def witness():
            _signal_witness(member, self.anchor, expected_pgid)
        while True:
            with self._intent(job, check_deadline=check_deadline, delivery=True):
                try:
                    witness()
                except OwnershipUnavailable as exc:
                    if not self.authority:
                        raise
                    first_failure = exc
                else:
                    if check_deadline and _present(self.directory/'deadline_expired.json'):
                        raise RequestSuperseded('Runner termination request pending verification')
                    yield
                    return
            # Register the actual failed observation, then retry reads outside
            # the transaction. Reacquire the gate even after a successful retry.
            self.authority.read_domain(witness, first_failure=first_failure)

    def _deliver(self, job, member, signum):
        if job['status'] != 'Run' or job.get('cancel_requested') or job.get('termination_requested'):
            raise RequestSuperseded('Termination superseded suspension signal')
        state = job['suspension']
        key = (state['revision'], (state.get('request') or {}).get('id'),
               tuple(sorted(member.items())), int(signum))
        delivered = getattr(self, 'delivered_signals', set())
        if key in delivered:
            return False
        if self.authority:
            self.authority.wait()
            _send(member, self.anchor, signum, authority=self.authority,
                  delivery=self._signal_gate(job, member, check_deadline=True))
        else:
            with self._intent(job, check_deadline=True, delivery=True):
                _send(member, self.anchor, signum)
        delivered.add(key)
        self.delivered_signals = delivered
        return True

    def _suspend(self, job):
        from .suspension_protocol import now_clock
        previous = None
        stable = 0
        limit = self._transition_time()+TRANSITION_TIMEOUT
        signalled = list((job['suspension'].get('evidence') or {}).get('signals', []))
        def deliver(member, signum):
            if not self._deliver(job, member, signum):
                return
            signalled.append({'identity': member, 'signal': int(signum), 'delivered': True})
            current = self.current()
            previous_evidence = dict(current['suspension'].get('evidence') or {})
            previous_evidence['signals'] = list(signalled)
            self.update(job, evidence=previous_evidence, signal_history=[*current['suspension'].get('signal_history', []), signalled[-1]])
        try:
            while self._transition_time() < limit:
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
                if root in domain.computational and not self._stopped(root):
                    deliver(root, signal.SIGSTOP)
                for launcher in domain.launchers:
                    deliver(launcher, signal.SIGTSTP)
                for member in domain.computational:
                    if not self._read(lambda: any(_descends(member['pid'], launcher['pid'])
                                                  for launcher in domain.launchers)) and not self._stopped(member):
                        deliver(member, signal.SIGSTOP)
                    elif domain.launchers and self.registration['strategy'] == 'orca-tree' and member == root:
                        pass
                time.sleep(0.05)
                check = self._validate()
                signature = _domain_members(check.computational)
                stopped = all(self._stopped(m) for m in check.computational)
                stable = stable+1 if stopped and signature == previous else 0
                previous = signature
                self.update(job, evidence={**self._evidence(check), 'signals': signalled})
                if stable >= 2:
                    return check, now_clock()
            self.update(job, reason='Awaiting computational stop verification; retained domain remains held')
            return None
        except Exception:
            # Already delivered STOPs remain in evidence; never roll them back.
            raise

    def prepare_cancel(self, job):
        """Inhibit runner producers before TERM and termination-only CONT."""
        if self.registration is None:
            return True
        with self._queue_transaction() as con:
            latest = self.store.get(con, self.job_id)
            if (latest['claim'] != self.claim or latest['worker'] != self.context['worker']
                    or not (latest.get('cancel_requested') or latest.get('termination_requested'))):
                raise OwnershipContradiction('Termination authority changed')
            state = latest['suspension']
            if (state.get('request') or {}).get('action') != 'cancel':
                state['revision'] += 1
                state['request'] = dict(id=uuid4().hex, action='cancel', revision=state['revision'],
                    attempt_id=latest['attempt_id'], authority=_authority(latest),
                    state_identity=self.context['state_identity'], requested_at=time.time())
                state.update(desired='Terminated', inhibited=True)
                self.store.save(con, latest)
        job = latest
        self.command(job, 'cancel')
        limit = self._transition_time()+2
        while self._transition_time() < limit:
            self.current()
            if self._read(lambda: _present(self.directory.parent/'payload-exit.json')) and self.ready_to_release():
                return False  # Receipt won the request/ACK race; release normally.
            acknowledged = self.ack(job, 'cancel', 'cancel_prepared')
            if acknowledged:
                self.update(job, intervals=acknowledged.get('intervals', []),
                            suspended_seconds=acknowledged.get('suspended_seconds'),
                            unsuspended_elapsed=acknowledged.get('unsuspended_elapsed'))
                self.cancel_intent = job
                return True
            time.sleep(0.05)
        raise OwnershipUnavailable('cancel.ack', 'Runner termination preparation is not yet acknowledged')

    def observe_session(self):
        """Retain original identities across launcher exit and reparenting."""
        self.current()
        def read_snapshot():
            return snapshot_owned_session(self.anchor, self.observed.values())
        snapshot = self.authority.read_domain(read_snapshot) if self.authority else read_snapshot()
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
                    raise OwnershipContradiction('Unregistered late computational member during cancellation')
            self.observed[pid] = row['identity']
            self.observed_groups.add(row['pgid'])
        for item in snapshot['pending']:
            self.observed[item['identity']['pid']] = item['identity']
        return snapshot

    def _groups_absent(self):
        for pgid in self.observed_groups - {self.anchor['pid']}:
            if group_exists(pgid):
                return False
        return True

    def ready_to_release(self):
        if self.registration is None or self.last_domain is None:
            raise CompletionDomainUnobserved()
        snapshot = self.observe_session()
        if snapshot['pending'] or {row['identity']['pid'] for row in snapshot['members']} != {self.anchor['pid']}:
            return False
        try:
            return self._read(self._groups_absent)
        except OwnershipUnavailable:
            if self.authority:
                raise
            return False  # A read-only projection cannot grant release on denial.

    def _cancel_send(self, member, signum):
        if self.authority:
            self.authority.wait()
        snapshot = self.observe_session()
        rows = {row['identity']['pid']: row for row in snapshot['members']}
        row = rows.get(member['pid'])
        if member in snapshot['absent']:
            return True
        if any(item['identity'] == member for item in snapshot['pending']):
            return False  # No syscall; observe the original until fresh or absent.
        if row is None or row['identity'] != member:
            raise OwnershipContradiction('Termination target cannot be freshly verified')
        if row['pgid'] == self.anchor['pid'] or member == self.registration['runner']:
            raise OwnershipContradiction('Protected control cannot be a termination target')
        if row['status'] in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return False  # Observe original reaping; never signal an exiting witness.
        self.current()
        key = (tuple(sorted(member.items())), signum)
        if key in self.termination_signals:
            return True
        event = {'identity': member, 'signal': int(signum), 'delivered': False,
                 'operation': 'identity-verification'}
        try:
            # Individual identities avoid signalling co-grouped runner controls.
            def target_read():
                try:
                    if identity_for(member) != member:
                        result = observe_owner(member)
                        if result.status != OwnershipStatus.GONE:
                            raise OwnershipUnavailable(result.deciding_check, 'Termination target identity is unreadable')
                        raise OwnershipContradiction('Termination target changed before signal delivery')
                    if os.getsid(member['pid']) != self.anchor['pid'] or os.getpgid(member['pid']) != row['pgid']:
                        raise OwnershipContradiction('Termination target changed before signal delivery')
                except psutil.ZombieProcess as exc:
                    raise OwnershipUnavailable('termination_target', 'Exiting target identity is unreadable') from exc
                except (ProcessLookupError, psutil.NoSuchProcess):
                    raise
                except (OSError, psutil.Error) as exc:
                    raise OwnershipUnavailable('termination_target', 'Termination identity cannot be read') from exc
            if self.authority:
                self.authority.read_domain(target_read)
            else:
                # Outside an owned worker, denial is retained as an unsent
                # operation for the existing read-only absence checks.
                try:
                    target_read()
                except OwnershipUnavailable as exc:
                    if isinstance(exc.__cause__, (PermissionError, psutil.AccessDenied)):
                        raise exc.__cause__
                    raise
            with self._signal_gate(self.cancel_intent, member, expected_pgid=row['pgid']):
                event['operation'] = 'signal-delivery'
                os.kill(member['pid'], signum)
                self.termination_signals.add(key)
            event['delivered'] = True
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess) as exc:
            event['errno'] = getattr(exc, 'errno', None)
            # No retry of the denied signal. Only independent original absence
            # AND kernel group absence can discharge this obligation.
            self.denied_signals.append((member, row['pgid'], self._transition_time(), exc))
        current = self.current()
        self.update(current, signal_history=[*current['suspension'].get('signal_history', []), event])
        return True

    def cancel_step(self, job, grace):
        try:
            self._flush_updates()
            self._flush_command()
            self._cancel_step(job, grace)
        except RequestSuperseded:
            self.cancellation_prepared = False
            self.cancel_intent = None
        except OwnershipUnavailable as exc:
            if self.authority:
                self.authority.read_domain(self._current, first_failure=exc)
            # The cancellation tick owns the next read; denied sends remain
            # recorded and are never repeated merely because a read failed.

    def _cancel_step(self, job, grace):
        """One primary initiator, then one bounded handoff to surviving members."""
        self.current()
        now = self._transition_time()
        if self.cancellation_started is None:
            self.cancellation_started = now
        if not self.cancellation_prepared:
            if self.registration is None:
                try:
                    if not self._registration(job):
                        if now-self.cancellation_started >= TRANSITION_TIMEOUT:
                            self.update(job, reason='Awaiting coordinated runner handshake for termination', inhibited=True)
                        return
                except DomainPending:
                    if now-self.cancellation_started >= TRANSITION_TIMEOUT:
                        self.update(job, reason='Awaiting a verifiable termination domain', inhibited=True)
                    return
            snapshot = self.observe_session()
            root = self.registration['root']
            root_exiting = (root in snapshot['absent'] or any(
                item['identity'] == root for item in snapshot['pending']) or any(
                row['identity'] == root and row['status'] in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
                for row in snapshot['members']))
            domain = self.last_domain if root_exiting else self._validate()
            if domain is None:
                raise OwnershipContradiction('No verified computational domain precedes root exit')
            prepared = self.prepare_cancel(job)
            self.current()
            if not prepared:
                return
            self.cancellation_prepared = True
            self.cancellation_started = self._transition_time()
            # MPI controls receive TERM once and own initial rank shutdown.
            # A stopped serial producer requires termination-only CONT after TERM.
            root = self.registration['root']
            self.primary_pending = [(launcher, signal.SIGTERM) for launcher in domain.launchers]
            if root not in domain.launchers:
                self.primary_pending.extend(((root, signal.SIGTERM), (root, signal.SIGCONT)))
        primary = getattr(self, 'primary_pending', [])
        if primary and not self.denied_signals:
            while primary:
                member, signum = primary[0]
                if not self._cancel_send(member, signum):
                    return
                primary.pop(0)
                if self.denied_signals:
                    return
            return
        snapshot = self.observe_session()
        for member, pgid, started, error in list(self.denied_signals):
            absent = member in snapshot['absent']
            gone = absent and self._read(lambda: not group_exists(pgid))
            if gone:
                self.denied_signals.remove((member, pgid, started, error))
            # A denied signal is never replayed. Presence or an unreadable
            # absence probe retains the original pending observation.
        if self.denied_signals:
            return
        if primary:
            # The next cancellation tick resumes the remaining primary list
            # after the denied operation was discharged by absence evidence.
            return
        if now-self.cancellation_started > grace:
            # Explicit fallback after the primary grace, never a second TERM
            # traversal racing MPI's initial shutdown. Preserve the receipt chain.
            protected = [self.registration['runner'], *self.registration.get('protected', [])]
            for row in snapshot['members']:
                member = row['identity']
                key = tuple(sorted(member.items()))
                if (row['pgid'] != self.anchor['pid'] and member not in protected
                        and key not in self.fallback_signalled):
                    if self._cancel_send(member, signal.SIGKILL):
                        self.fallback_signalled.add(key)
                    if self.denied_signals:
                        break
            self.cancellation_escalated = True
        if now-self.cancellation_started > grace+TRANSITION_TIMEOUT:
            self.update(job, reason='Awaiting owned drainage and a genuine payload receipt', inhibited=True)

    def verify_released(self):
        """One post-release read; the worker's completion driver owns retries."""
        self.current(completion=True)
        pending = False
        for member in self.observed.values():
            try:
                current = identity_for(member)
            except psutil.ZombieProcess:
                pending = True
            except (ConfirmedProcessAbsence, psutil.NoSuchProcess):
                continue
            except ProcessLookupError:
                pending = True
            except (OSError, psutil.Error) as exc:
                raise OwnershipUnavailable('released.identity', 'Released identity temporarily unreadable') from exc
            else:
                # A reused numeric PID proves the original v2 identity ended;
                # it grants no authority over the new process.
                if current != member:
                    if (member.get('version', 1) != 2 or current.get('host') != member['host']
                            or current.get('pid') != member['pid']):
                        raise OwnershipUnavailable('released.identity', 'Legacy released identity differs')
                    continue
                pending = True
        return not (any(group_exists(group) for group in self.observed_groups) or pending)

    def tick(self, job, *, receipt_present=False):
        if receipt_present and (self.registration is None or self.last_domain is None):
            # This is an evidence limit, not proof of loss or permission for
            # retrospective registration. The worker owns the visible hold.
            raise CompletionDomainUnobserved()
        try:
            self._flush_updates()
            self._flush_command()
            self._tick(self.current())
        except RequestSuperseded:
            # A client may commit intent during a routine observation or error
            # report. Preserve that intent and re-read it on the next pass.
            return
        except OwnershipUnavailable as exc:
            if self.authority:
                self.authority.read_domain(self._current, first_failure=exc)
            return
        except Exception as exc:
            if not is_unavailable(exc):
                raise
            # Commands/updates retain their exact pending intent. Before the
            # next phase pass, hold and re-establish the read-only queue binding.
            if self.authority:
                self.authority.read_domain(self._current, first_failure=exc)
                return
            raise OwnershipUnavailable('suspension.read', 'Suspension observation temporarily unavailable') from exc

    def _tick(self, job):
        from .suspension_protocol import now_clock
        now = self._read(now_clock)
        phase = job['suspension']['phase']
        try:
            if self.registration is None:
                if not self._registration(job):
                    if now-self.started > TRANSITION_TIMEOUT:
                        self.update(job, reason='Runner handshake unavailable; suspension unsupported for this attempt')
                    return
                job = self.current()
            expired = self._channel_read('deadline_expired.json')
            if expired is not None:
                if expired.get('runner') != self.registration['runner']:
                    raise OwnershipContradiction('Foreign deadline owner')
                with self._queue_transaction() as con:
                    latest = self.store.get(con, self.job_id)
                    if latest['claim'] == self.claim and latest['status'] == 'Run':
                        latest.update(status='Cancelling', termination_requested=expired,
                                      reason='Declared execution deadline expired')
                        latest['suspension']['inhibited'] = True
                        self.store.save(con, latest)
                return
            if phase in {'Run', 'Suspending', 'Resuming'} and self._read(self._root_exited):
                # A genuine root exit can precede the shell receipt. Keep the
                # active commitment while the coordinated runner drains modules
                # and finalizes; an exit race is never a successful suspension.
                self.completion_pending_since = self.completion_pending_since or self._transition_time()
                self.update(job, reason='Awaiting genuine runner completion and owned-session drainage',
                            inhibited=True, evidence=None, verified_at=None)
                return
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
                # exit enters the observation driver for genuine completion.
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
                if _domain_members(old) != _domain_members(domain.computational) or not all(self._stopped(m) for m in domain.computational):
                    raise OwnershipContradiction('Suspended membership lost, changed, or externally continued')
                ack = self.ack(job, 'suspended', 'paused')
                # The runner can refresh its acknowledgement during validation.
                checked_at = now_clock()
                if not ack or not isinstance(ack.get('observed_clock'), (int, float)) or not 0 <= checked_at-ack['observed_clock'] <= 3:
                    raise OwnershipUnavailable('suspended.ack', 'Fresh suspended runner/deadline acknowledgement unavailable')
                self.update(job, evidence=self._evidence(domain), verified_at=time.time(),
                            intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                            unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                return
            request_id = job['suspension']['request']['id']
            if self.pending is None or self.pending['id'] != request_id:
                operation = 'prepare_suspend' if phase == 'Suspending' else 'prepare_continue'
                boundary = now if phase == 'Resuming' else None
                self.pending = {'id':request_id,'stage':operation,'started':self._transition_time(), 'boundary':boundary}
                self.command(job, operation, boundary)
                return
            if self._transition_time()-self.pending['started'] > TRANSITION_TIMEOUT+2:
                self.update(job, reason='Awaiting runner transition acknowledgement; partial state retained')
            stage = self.pending['stage']
            if stage == 'prepare_suspend' and self.ack(job, stage, 'prepared'):
                result = self._suspend(job)
                if result is None:
                    return
                domain, boundary = result
                self.update(job, evidence=self._evidence(domain))
                self.pending.update(stage='suspended', boundary=boundary)
                self.command(job, 'suspended', boundary)
            elif stage == 'suspended':
                ack = self.ack(job, stage, 'paused')
                if ack:
                    domain = self._validate()
                    if not all(self._stopped(m) for m in domain.computational):
                        raise OwnershipContradiction('Stop state changed before suspension acknowledgement')
                    self.update(job, phase='Suspended', evidence=self._evidence(domain), verified_at=time.time(),
                                intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                                unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                    self.pending = None
            elif stage == 'prepare_continue' and (prepared := self.ack(job, stage, 'resume_prepared')):
                self.update(job, intervals=prepared.get('intervals', []), suspended_seconds=prepared.get('suspended_seconds'),
                            unsuspended_elapsed=prepared.get('unsuspended_elapsed'))
                domain = self._validate()
                old = job['suspension']['evidence']['computational']
                if _domain_members(old) != _domain_members(domain.computational) or not all(self._stopped(m) for m in domain.computational):
                    raise OwnershipContradiction('Continue requires the same stopped computational identities')
                if self._cancel_pending():
                    return
                # Release MPI ranks before serial driver, preserving the held
                # runner until runnable-state verification has completed.
                for launcher in domain.launchers:
                    self._deliver(job, launcher, signal.SIGCONT)
                for member in reversed(domain.computational):
                    self._deliver(job, member, signal.SIGCONT)
                self.pending.update(stage='verify_continue', domain=domain)
            elif stage == 'verify_continue':
                domain = self._validate()
                prior = self.pending['domain'].computational
                if _domain_members(prior) != _domain_members(domain.computational):
                    raise OwnershipContradiction('Computational identities changed during Continue verification')
                if any(self._stopped(m) for m in domain.computational):
                    return
                self.pending.update(stage='run')
                self.command(job, 'run')
            elif stage == 'run':
                ack = self.ack(job, stage, 'running')
                if ack:
                    domain = self._validate()
                    prior = self.pending['domain'].computational
                    if _domain_members(prior) != _domain_members(domain.computational) or any(self._stopped(m) for m in domain.computational):
                        raise OwnershipContradiction('Continue acknowledgement lost original computational domain')
                    self.update(job, phase='Run', inhibited=False, evidence=self._evidence(domain), verified_at=time.time(),
                                intervals=ack.get('intervals', []), suspended_seconds=ack.get('suspended_seconds'),
                                unsuspended_elapsed=ack.get('unsuspended_elapsed'))
                    self.pending = None
        except (RequestSuperseded, OwnershipUnavailable):
            raise
        except (JobsError, OSError, ValueError, KeyError, psutil.Error) as exc:
            if is_unavailable(exc):
                raise OwnershipUnavailable('suspension.read', 'Suspension observation temporarily unavailable') from exc
            if isinstance(exc, DomainPending):
                self.domain_pending_since = self.domain_pending_since or self._transition_time()
                self.update(job, reason=str(exc), evidence=None, verified_at=None)
                return
            if not isinstance(exc, JobsError):
                raise OwnershipUnavailable('suspension.internal', 'Suspension observation temporarily unavailable') from exc
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
