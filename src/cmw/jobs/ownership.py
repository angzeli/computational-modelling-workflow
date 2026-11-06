"""Kernel locks and birth identity; never identify ownership by engine name."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
import errno
import fcntl
import math
import os
from pathlib import Path
import socket
import time
from uuid import UUID

import psutil

from . import process_identity
from .process_identity import boot_session_id
from .store import JobsError, private_opener


class OwnershipStatus(StrEnum):
    VERIFIED = 'VERIFIED'
    GONE = 'GONE'
    UNVERIFIABLE = 'UNVERIFIABLE'


@dataclass
class OwnershipObservation:
    status: OwnershipStatus
    deciding_check: str
    record_version: int | None
    boot_session: str | None
    checks: dict

    def as_dict(self):
        return asdict(self)


def identity(pid=None):
    # Switch new attempts after every control path handles all three outcomes.
    return _legacy_identity(os.getpid() if pid is None else pid)


def _legacy_identity(pid):
    process = psutil.Process(pid)
    return {'pid': process.pid, 'birth': process.create_time(), 'host': socket.gethostname(),
            'boot': psutil.boot_time()}


def identity_for(record, pid=None):
    """Recapture in the originating format, including members of a v1 session."""
    pid = record['pid'] if pid is None else pid
    if record.get('version', 1) == 1 and 'boot' in record:
        return _legacy_identity(pid)
    if record.get('version') == 2:
        return process_identity.capture(pid)
    return identity(pid)


def valid_owner_record(record):
    if not isinstance(record, dict):
        return False
    version = record.get('version', 1)
    if (type(version) is not int or type(record.get('pid')) is not int or
            not 0 < record['pid'] <= 2**31 - 1 or not isinstance(record.get('host'), str)
            or not record['host']):
        return False
    if version == 1:
        return all(type(record.get(key)) is int or
                   (type(record.get(key)) is float and math.isfinite(record[key]))
                   for key in ('boot', 'birth'))
    if version != 2 or type(record.get('start_token')) is not int or record['start_token'] < 0:
        return False
    try:
        return str(UUID(record['boot_session'])) == record['boot_session']
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def observe_owner(record):
    """Observe ownership. Absence needs positive evidence on the recorded host."""
    status = OwnershipStatus
    version = record.get('version', 1) if isinstance(record, dict) else None
    current_boot_session = None
    checks = {}

    def check(name, recorded, observed, result, reason):
        checks[name] = {'recorded': recorded, 'observed': observed, 'result': result, 'reason': reason}

    def finish(result, name):
        return OwnershipObservation(result, name, version, current_boot_session, checks)

    if not valid_owner_record(record):
        check('record', record, None, status.UNVERIFIABLE, 'Incomplete or unsupported identity record')
        return finish(status.UNVERIFIABLE, 'record')
    pid = record['pid']
    boot_key, start_key = ('boot_session', 'start_token') if version == 2 else ('boot', 'birth')
    for name in ('host', boot_key, 'pid', start_key, 'process_status'):
        expected = 'live' if name == 'process_status' else record[name]
        check(name, expected, None, status.UNVERIFIABLE, 'Not observed')
    field = 'host'
    try:
        host = socket.gethostname()
        if host != record['host']:
            check(field, record[field], host, status.UNVERIFIABLE, 'Recorded host differs from this host')
            return finish(status.UNVERIFIABLE, field)
        check(field, record[field], host, status.VERIFIED, 'Host matches')
        field = boot_key
        if version == 2:
            boot = current_boot_session = boot_session_id()
            if str(UUID(boot)) != boot:
                raise ValueError('Incomplete kernel boot UUID')
        else:
            # Advisory context for v1 diagnosis. The boot UUID was never recorded by v1.
            try:
                current_boot_session = boot_session_id()
            except Exception:
                pass
            boot = psutil.boot_time()
        if boot != record[field]:
            result = status.GONE if version == 2 else status.UNVERIFIABLE
            check(field, record[field], boot, result, 'Boot session differs' if version == 2 else
                  'Legacy wall-clock boot timestamp differs')
            return finish(result, field)
        check(field, record[field], boot, status.VERIFIED, 'Boot evidence matches')
        field = start_key
        if version == 2:
            token, ended = process_identity.process_evidence(pid)
            if type(token) is not int or token < 0 or type(ended) is not bool:
                raise ValueError('Incomplete kernel process evidence')
        else:
            process = psutil.Process(pid)
            token = process.create_time()
        check('pid', pid, pid, status.VERIFIED, 'PID observed')
        if token != record[field]:
            result = status.GONE if version == 2 else status.UNVERIFIABLE
            check(field, record[field], token, result, 'Kernel start token differs' if version == 2 else
                  'Legacy adjusted process birth differs')
            return finish(result, field)
        check(field, record[field], token, status.VERIFIED, 'Start evidence matches')
        if version == 1:
            field = 'process_status'
            ended = process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
            field = start_key
            after = psutil.Process(pid).create_time()
            if after != token:
                check(field, record[field], after, status.UNVERIFIABLE, 'Legacy birth changed during observation')
        check('process_status', 'live', 'zombie/dead' if ended else 'live',
              status.UNVERIFIABLE if ended else status.VERIFIED,
              'Process has exited but still occupies its PID' if ended else 'Process is live')
    except (psutil.ZombieProcess, PermissionError, psutil.AccessDenied) as exc:
        check(field, checks[field]['recorded'], None, status.UNVERIFIABLE, type(exc).__name__)
        return finish(status.UNVERIFIABLE, field)
    except (psutil.NoSuchProcess, ProcessLookupError) as exc:
        # ESRCH from a boot or host query cannot establish process absence.
        if field in (start_key, 'process_status'):
            check('pid', pid, None, status.GONE, 'Kernel confirms PID is absent')
            return finish(status.GONE, 'pid')
        check(field, checks[field]['recorded'], None, status.UNVERIFIABLE, type(exc).__name__)
        return finish(status.UNVERIFIABLE, field)
    except Exception as exc:
        check(field, checks[field]['recorded'], None, status.UNVERIFIABLE, type(exc).__name__)
        return finish(status.UNVERIFIABLE, field)

    for field, row in checks.items():
        if row['result'] != status.VERIFIED:
            return finish(row['result'], field)
    return finish(status.VERIFIED, 'identity')


def owner_alive(owner):
    """Positive-authority predicate. A false result is never evidence of absence."""
    return observe_owner(owner).status is OwnershipStatus.VERIFIED


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a+", opener=private_opener) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobsError(f"Already owned: {path.name}") from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def lock_held(path, *, strict=False):
    if not path.exists():
        return False
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        if strict:
            raise
        return True  # Inaccessible ownership must not authorize dispatch.


def group_members(pgid, *, pids=None, process_factory=psutil.Process, session=False):
    """Only inspect membership of the group we created. No engine-name discovery."""
    members = []
    membership = os.getsid if session else os.getpgid
    for pid in psutil.pids() if pids is None else pids:
        # POSIX PID 0 means the caller to getsid/getpgid, not the kernel row.
        if pid <= 0:
            continue
        try:
            if membership(pid) == pgid and process_factory(pid).status() != psutil.STATUS_ZOMBIE:
                members.append(pid)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as exc:
            # A group member whose state is unreadable cannot be declared ended.
            try:
                if membership(pid) == pgid:
                    raise JobsError("Managed process group is not fully observable") from exc
            except ProcessLookupError:
                pass
    return members


def snapshot_owned_session(owner, known=()):
    """Observe an anchored session without equating exit races with drainage.

    ``known`` contains original birth identities retained by the cancellation
    coordinator. A readable original whose topology is disappearing remains
    pending; zombies remain members. This function never sends a signal.
    """
    sid = owner['pid']
    originals = {}
    for member in known:
        if member['pid'] in originals and originals[member['pid']] != member:
            raise JobsError('Conflicting original session identities')
        originals[member['pid']] = member
    if sid in originals and originals[sid] != owner:
        raise JobsError('Protected session identity differs from original evidence')
    originals[sid] = owner
    result = {'members': [], 'absent': [], 'pending': []}

    def verify_anchor():
        try:
            if (identity_for(owner) != owner or os.getsid(sid) != sid or os.getpgid(sid) != sid
                    or psutil.Process(sid).status() in
                    {psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP,
                     psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
                    or identity_for(owner) != owner):
                raise JobsError('Protected session authority is unavailable')
        except (OSError, psutil.Error) as exc:
            raise JobsError('Protected session authority is unavailable') from exc

    def pending(member, reason):
        result['pending'].append({'identity': member, 'reason': reason})

    def disappearance(pid, member):
        # A failed getsid/getpgid is not independently verified process exit.
        try:
            current = identity_for(member)
        except psutil.ZombieProcess:
            pending(member, 'Original process has unreadable zombie metadata')
        except (psutil.NoSuchProcess, process_identity.ConfirmedProcessAbsence):
            if pid in originals:
                result['absent'].append(originals[pid])
        except ProcessLookupError:
            pending(member, 'Original identity query returned ESRCH without independent absence')
        except (PermissionError, psutil.AccessDenied) as exc:
            raise JobsError('Original session member is not fully observable') from exc
        else:
            if current != member:
                raise JobsError('Original session member identity changed')
            pending(member, 'Original birth identity remains readable while topology is unavailable')

    verify_anchor()
    for pid in sorted(set(psutil.pids()) | originals.keys()):
        if pid <= 0:
            continue
        original = originals.get(pid)
        if original is None:
            # Do not inspect or adopt an unrelated process merely because its
            # PID appeared in the system enumeration.
            try:
                if os.getsid(pid) != sid:
                    continue
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise JobsError('Session membership is not fully observable') from exc
        member = original
        try:
            current = identity_for(original or owner, pid)
            if original is not None and current != original:
                raise JobsError('Original session member identity changed')
            member = current
            if os.getsid(pid) != sid:
                raise JobsError('Original session member escaped its owned session')
            pgid = os.getpgid(pid)
            process = psutil.Process(pid)
            status, ppid = process.status(), process.ppid()
            if identity_for(member) != member:
                raise JobsError('Session member identity changed during observation')
            if os.getsid(pid) != sid or os.getpgid(pid) != pgid:
                raise JobsError('Session member topology changed during observation')
        except psutil.ZombieProcess:
            if member is None:
                raise JobsError('Session zombie identity cannot be verified')
            pending(member, 'Original process has unreadable zombie metadata')
        except (ProcessLookupError, psutil.NoSuchProcess):
            if member is None:
                continue
            disappearance(pid, member)
        except (PermissionError, psutil.AccessDenied) as exc:
            raise JobsError('Original session member is not fully observable') from exc
        else:
            result['members'].append({'identity': member, 'pgid': pgid,
                                      'status': status, 'ppid': ppid})
    verify_anchor()
    return result


def signal_session(owner, signum, *, include_leader=True, absence_grace=0):
    """Signal birth-verified groups in an explicitly owned session.

    A trusted sole foreground payload may exclude the protected leader group
    when draining its child subgroups. The caller must establish that exclusive
    topology before launching children; this option grants no new ownership.
    """
    sid = owner['pid']

    def verify_owner():
        if not owner_alive(owner) or os.getsid(sid) != sid or os.getpgid(sid) != sid:
            raise JobsError('Dedicated session ownership lost; no signal sent')

    def await_absence(pgid):
        # Observation only: a reaping group can briefly be positive or denied.
        deadline = time.monotonic()+absence_grace
        while True:
            verify_owner()
            try:
                if not group_exists(pgid):
                    return
            except JobsError as exc:
                if not isinstance(exc.__cause__, PermissionError) or time.monotonic() >= deadline:
                    raise
            else:
                if time.monotonic() >= deadline:
                    raise JobsError('Session subgroup has no verified surviving member; no signal sent')
            time.sleep(min(0.05, max(0, deadline-time.monotonic())))

    def witness_exited(member):
        # owner_alive(False) also includes access denial and cannot prove exit.
        try:
            if identity_for(member) != member:
                return False
            return (psutil.Process(member['pid']).status() == psutil.STATUS_ZOMBIE
                    and identity_for(member) == member)
        except psutil.ZombieProcess:
            return False  # An unreadable zombie is not a birth-verified zombie.
        except (ProcessLookupError, psutil.NoSuchProcess):
            return True
        except (PermissionError, psutil.AccessDenied):
            return False

    verify_owner()
    groups = {sid: [owner]}
    for pid in group_members(sid, session=True):
        try:
            member = identity_for(owner, pid)
            pgid = os.getpgid(pid)
            if os.getsid(pid) != sid:
                raise JobsError('Session subgroup identity cannot be verified')
            groups.setdefault(pgid, []).append(member)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    for pgid in sorted(groups.keys() - {sid}) + ([sid] if include_leader else []):
        verify_owner()
        for member in groups[pgid]:
            if not owner_alive(member):
                continue
            try:
                if os.getsid(member['pid']) != sid or os.getpgid(member['pid']) != pgid:
                    raise JobsError('Session subgroup identity cannot be verified')
                verify_owner()
                if not owner_alive(member):
                    continue
            except ProcessLookupError:
                continue
            # A current birth-verified member witnesses a leaderless group.
            # Numeric PGID alone never authorizes a signal.
            try:
                os.killpg(pgid, signum)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                if exc.errno != errno.EPERM or absence_grace <= 0 or not witness_exited(member):
                    raise
                # A denied send is not retried. Only subsequently proven group
                # absence permits moving on; live or ambiguous groups refuse.
                await_absence(pgid)
            break
        else:
            await_absence(pgid)



def group_exists(pgid):
    """Kernel-level final gate after reaping our leader; enumeration can race fork."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise JobsError("Process group absence cannot be verified") from exc
    return True


def observed_group_members(store, job, pids, *, process_factory=psutil.Process, identity_provider=identity):
    """Use the existing guardian lock and dedicated process group contract.

The live locked guardian owns surviving group children after the leader exits.
A live replacement with the original leader PID is never accepted.
"""
    group = job.get('group')
    if pids is None or not group or not owner_alive(job.get('worker')):
        return [], 'Managed membership unavailable'
    if not lock_held(store.root/'attempts'/job['attempt_id']/'worker.lock', strict=True):
        return [], 'Managed guardian lock unavailable'
    def read_identity(pid):
        return identity_for(group, pid) if identity_provider is identity else identity_provider(pid)

    try:
        if read_identity(group['pid']) != group:
            return [], 'Managed group leader identity changed'
    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    members = []
    session = job.get('env', {}).get('CMW_JOBS_OWN_SESSION') == '1'
    for pid in group_members(group['pid'], pids=pids, process_factory=process_factory, session=session):
        try:
            owner = read_identity(pid)
            boot_key = 'boot_session' if group.get('version') == 2 else 'boot'
            if (owner['host'] != group['host'] or owner.get(boot_key) != group[boot_key] or
                    os.getsid(pid) != group['pid'] or (not session and os.getpgid(pid) != group['pid'])):
                continue
            members.append({'identity': owner, 'pgid': os.getpgid(pid)})
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return members, ''
