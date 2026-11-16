"""Local administrative closure of Unknown attempts; never execution authority."""
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import time

from .lifecycle import _read_file
from .ownership import OwnershipStatus, group_members, observe_owner
from .store import JobsError


class RetirementRefused(JobsError):
    def __init__(self, reason, *, unavailable=False, evidence=None):
        super().__init__(reason)
        self.exit_code = 75 if unavailable else 2
        self.result = {'status': 'UNVERIFIABLE' if unavailable else 'REFUSED',
                       'reason': reason, 'exit_code': self.exit_code,
                       'evidence': evidence or {}}


def _refuse(name, reason, *, unavailable=True, evidence=None):
    raise RetirementRefused(f'{name}: {reason}', unavailable=unavailable, evidence=evidence)


def _identities(value, path, result, *, required=False):
    """Keep every occurrence, including identities in retained domain evidence."""
    if required and not (isinstance(value, dict) and 'identity' in value):
        result[path] = value
    if isinstance(value, dict):
        if 'pid' in value or 'start_token' in value or 'birth' in value:
            result[path] = value
        slots = set()
        if path == 'job.suspension.capability':
            slots = {'runner'}
        elif path.endswith('/suspension/context.json'):
            slots = {'worker', 'anchor'}
        elif path.endswith('/suspension/registration.json'):
            slots = {'runner', 'root'}
        elif path.endswith(('/suspension/ack.json', '/suspension/deadline_expired.json')):
            slots = {'runner'}
        for key in slots - value.keys():
            result[f'{path}.{key}'] = None
        for key, item in value.items():
            label = f'{path}.{key}'
            if key in {'members', 'descendants', 'recorded_descendants', 'protected', 'computational',
                       'launchers', 'computational_scope', 'protected_scope'}:
                if not isinstance(item, list):
                    result[label] = item  # An incomplete member collection refuses.
                else:
                    for index, member in enumerate(item):
                        _identities(member, f'{label}[{index}]', result, required=True)
            else:
                _identities(item, label, result,
                            required=key in slots or (required and key == 'identity'))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _identities(item, f'{path}[{index}]', result)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate metadata key: {key}')
        result[key] = value
    return result


def _attempt_metadata(store, attempt):
    """Only CMW attempt JSON; never follow job/output paths or symlinks."""
    records = {}
    for prefix in [('attempts', attempt), ('attempts', attempt, 'suspension')]:
        directory = store.root.joinpath(*prefix)
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                _refuse(str(directory), 'Attempt metadata directory is redirected or not a directory')
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            if len(prefix) == 2:
                _refuse('/'.join(prefix), 'Recorded attempt directory is missing; '
                        'its ownership metadata cannot be established')
            continue
        for entry in entries:
            if not entry.name.endswith('.json'):
                continue
            parts = (*prefix, entry.name)
            with _read_file(store, parts) as descriptor:
                if os.fstat(descriptor).st_size > 1024 * 1024:
                    _refuse('/'.join(parts), 'Attempt metadata exceeds the observation limit')
                payload = os.read(descriptor, 1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    _refuse('/'.join(parts), 'Attempt metadata exceeds the observation limit')
                value = json.loads(payload, object_pairs_hook=_json_pairs)
                if not isinstance(value, dict):
                    _refuse('/'.join(parts), 'Attempt metadata must be a JSON object')
            records['/'.join(parts)] = {
                'sha256': hashlib.sha256(payload).hexdigest(), 'value': value}
    return records


def _lock_existing(stack, store, parts, evidence):
    name = '/'.join(parts)
    try:
        descriptor = stack.enter_context(_read_file(store, parts))
    except FileNotFoundError:
        evidence[name] = 'absent'
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _refuse(name, 'Ownership lock is held; stop the matching supervisor before retirement',
                unavailable=False)
    # Closing revalidates the name and releases the lock before COMMIT;
    # the queue writer gate remains held through both boundaries.
    evidence[name] = 'exclusively held by retirement'


def _domain_ids(value, result):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'pgid', 'sid', 'session_id', 'process_group'}:
                if type(item) is not int or item <= 0:
                    _refuse(key, 'Malformed recorded process-domain identifier')
                result.add(('session' if key in {'sid', 'session_id'} else 'group', item))
            elif key in {'pgids', 'process_groups', 'session_ids'}:
                if not isinstance(item, list):
                    _refuse(key, 'Malformed recorded process-domain list')
                for number in item:
                    if type(number) is not int or number <= 0:
                        _refuse(key, 'Malformed recorded process-domain identifier')
                    result.add(('session' if key == 'session_ids' else 'group', number))
            else:
                _domain_ids(item, result)
    elif isinstance(value, list):
        for item in value:
            _domain_ids(item, result)


def _gone_evidence(job, control, metadata):
    records = {}
    _identities(job, 'job', records)
    if control.get('owner') is not None:
        records['controller.owner'] = control['owner']
    for name, record in metadata.items():
        _identities(record['value'], name, records)
    # A missing anchor after a spawn crash cannot prove there is no orphaned
    # execution domain. This operation supplies no inferred/adopted identity.
    if not isinstance(job.get('group'), dict):
        _refuse('job.group', 'No recorded execution domain; manual verification is required; '
                'retirement cannot invent missing ownership evidence')
    for role in ('worker', 'group'):
        if job.get(role) is not None:
            records[f'job.{role}'] = job[role]
    evidence = {'identities': {}, 'domains': [], 'metadata': metadata}
    for name, owner in records.items():
        observation = observe_owner(owner)
        row = observation.as_dict()
        evidence['identities'][name] = {'recorded': owner, 'observation': row}
        if observation.status != OwnershipStatus.GONE:
            reason = row['checks'].get(observation.deciding_check, {}).get('reason', observation.deciding_check)
            remedy = 'Keep dispatch paused; resolve the live or unreadable owner and retry.'
            if observation.record_version == 1 and observation.deciding_check in {'boot', 'birth'}:
                remedy = ('Legacy v1 mismatch is UNVERIFIABLE, never absence. Do not rewrite the identity '
                          'or signal a reused PID. Retry after its current occupant naturally exits; '
                          'a boot mismatch needs separately qualified earlier-boot proof, which this '
                          'command does not support.')
            _refuse(name, f'{observation.status}: {reason}. {remedy}',
                    unavailable=observation.status == OwnershipStatus.UNVERIFIABLE, evidence=evidence)
    domains = {('session', job['group']['pid']), ('group', job['group']['pid'])}
    _domain_ids(job, domains)
    for record in metadata.values():
        _domain_ids(record['value'], domains)
    # A previous boot's numeric SID/PGID may now name an unrelated process.
    # Boot-session proof establishes that none of the old domain survived.
    previous_boot = all(row['observation']['record_version'] == 2 and
                        row['observation']['deciding_check'] == 'boot_session'
                        for name, row in evidence['identities'].items() if name != 'controller.owner')
    for kind, number in sorted(domains):
        members = [] if previous_boot else group_members(number, session=kind == 'session')
        row = {'kind': kind, 'id': number, 'members': members,
               'result': 'GONE' if not members else 'LIVE',
               'basis': 'previous boot session' if previous_boot else 'current process census'}
        evidence['domains'].append(row)
        if members:
            _refuse(f'{kind} {number}', f'Live members remain: {members}; no signal was sent',
                    unavailable=False, evidence=evidence)
    return evidence


def retire(store, job_id, *, reason):
    """Commit one non-success disposition, only with complete GONE evidence."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096 or '\0' in reason:
        _refuse('reason', 'Supply nonempty text (at most 4096 characters)', unavailable=False)
    try:
        store.ensure_private_root(create=False)
        # Same BEGIN IMMEDIATE gate as controller/worker mutations; no schema
        # initialization, migration, queue re-enrollment, or lock-file creation.
        with store._writer(None) as con:
            schema = store._schema(con)
            if schema not in {2, 3, 4}:
                _refuse('queue', 'Retirement supports schemas 2, 3 and 4 without migration')
            job, control = store.get(con, job_id), store.control(con)
            retired = job.get('retirement')
            if retired is not None:
                if (not isinstance(retired, dict) or job['status'] != 'Fail'
                        or retired.get('attempt_id') != job['attempt_id']):
                    _refuse('retirement', 'Recorded disposition is inconsistent')
                return {'status': 'ALREADY_RETIRED', 'job_id': job['display_id'],
                        'job_status': job['status'], 'retirement': retired}
            if job['status'] != 'Unknown':
                _refuse('job.status', f"Expected Unknown, found {job['status']}", unavailable=False)
            attempt = job.get('attempt_id')
            if not isinstance(attempt, str) or re.fullmatch('[0-9a-f]{32}', attempt) is None:
                _refuse('job.attempt_id', 'Invalid attempt identity')
            with ExitStack() as locks:
                lock_evidence = {}
                for parts in [('controller.lock',), ('attempts', attempt, 'worker.lock'),
                              ('attempts', attempt, 'suspension', 'runner.lock')]:
                    _lock_existing(locks, store, parts, lock_evidence)
                metadata = _attempt_metadata(store, attempt)
                if (job.get('suspension') or {}).get('capability') is not None:
                    for name in ('context.json', 'registration.json'):
                        key = f'attempts/{attempt}/suspension/{name}'
                        if key not in metadata:
                            _refuse(key, 'Recorded coordinated capability has lost its ownership metadata')
                evidence = _gone_evidence(job, control, metadata)
                if _attempt_metadata(store, attempt) != metadata:
                    _refuse('attempt', 'Ownership metadata changed during observation')
                evidence['locks'] = lock_evidence
                now = time.time()
                retired = {'version': 1, 'attempt_id': attempt, 'claim': job.get('claim'),
                           'reason': reason.strip(), 'time': now,
                           'operator': {'uid': os.getuid(), 'euid': os.geteuid(),
                                        'name': pwd.getpwuid(os.geteuid()).pw_name},
                           'previous_status': 'Unknown', 'execution_outcome': 'unknown',
                           'evidence': evidence}
                job.update(status='Fail', reason='Administratively retired; execution outcome unknown: ' + reason.strip(),
                           finished_at=None, exit_code=None, signal=None, order=None, retirement=retired)
                job.pop('ownership_holds', None)
                job.pop('ownership_resume_status', None)
                store.save(con, job)
                store.event(con, job['id'], 'Unknown job retired: ' + json.dumps(retired, sort_keys=True))
                # Dispatch policy stays unchanged. An explicit resume/start can
                # now pass this attempt's admission gate, subject to other jobs.
            # Descriptor/name validation on ExitStack exit precedes COMMIT.
            # The queue writer gate remains held, so a newly starting owner
            # cannot register or dispatch across this boundary.
            store._commit(con, None)
            return {'status': 'RETIRED', 'job_id': job['display_id'],
                    'job_status': 'Fail', 'retirement': retired}
    except RetirementRefused:
        raise
    except (OSError, JobsError, ValueError, TypeError, KeyError) as exc:
        _refuse('retirement observation', f'{type(exc).__name__}: {exc}; keep dispatch paused and retry')
