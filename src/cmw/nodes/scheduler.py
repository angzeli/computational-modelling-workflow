"""Read-only PBS Pro observation for scheduler-backed nodes (issue #81 Phase 1).

PBS, not CMW, owns admission, placement and dispatch on these nodes. This
backend answers only ``capabilities`` and ``status``. It lists the endpoint
user's own PBS jobs as scheduler-reported rows; it never submits, signals or
adopts a job, and it opens no queue database.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import pwd
import re
import subprocess
import threading
import time
from uuid import uuid4

from .protocol import NodeError, OPERATIONS as PROTOCOL_OPERATIONS

OPERATIONS = ('capabilities', 'status')
POLL_INTERVAL_SECONDS = 30
MAX_JOBS = 1000
MAX_OUTPUT = 16 * 1024 * 1024
QUERY_TIMEOUT = 30
JOB_ID = re.compile(r'[0-9]{1,12}(?:\[[0-9]{0,12}\])?\.[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
# PBS Pro job_state letters, grouped for counts. Rows keep the verbatim letter.
PHASES = {'Q': 'queued', 'H': 'queued', 'W': 'queued', 'T': 'queued', 'M': 'queued',
          'R': 'running', 'E': 'running', 'B': 'running', 'S': 'running', 'U': 'running',
          'F': 'finished', 'X': 'finished'}
REQUESTED = ('select', 'ncpus', 'mpiprocs', 'mem', 'walltime', 'nodect', 'place')
USED = ('walltime', 'cput', 'mem', 'ncpus', 'cpupercent')
CAPABILITY = {'schema': 1, 'kind': 'pbs', 'read_only': True, 'poll_interval_seconds': POLL_INTERVAL_SECONDS}


class SchedulerError(NodeError):
    def __init__(self, code, message):
        super().__init__(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _text(value, limit):
    from cmw.jobs.cli import safe_text
    return None if value is None else safe_text(value)[:limit]


def _scalar(value, limit=64):
    if type(value) is int and abs(value) <= 2**53:
        return value
    return _text(value, limit) if isinstance(value, str) else None


def _query(argv, run):
    """One bounded PBS client call with a fixed argv; stdin is never inherited."""
    try:
        completed = run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=QUERY_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'{os.path.basename(argv[0])} timed out') from exc
    except OSError as exc:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'{os.path.basename(argv[0])} could not run: {exc}') from exc
    if len(completed.stdout) > MAX_OUTPUT:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', 'PBS output exceeds its bound')
    return completed


def _failure(completed, argv):
    reason = _text(completed.stderr.decode('utf-8', errors='replace').strip(), 512) or 'no diagnostic'
    return SchedulerError('SCHEDULER_UNAVAILABLE',
                          f'{os.path.basename(argv[0])} exited {completed.returncode}: {reason}')


def own_job_ids(qselect, user, *, run=subprocess.run):
    """Current and history-retained job IDs of one user, oldest first, at most MAX_JOBS."""
    argv = [qselect, '-x', '-u', user]
    completed = _query(argv, run)
    if completed.returncode != 0:
        raise _failure(completed, argv)
    ids = []
    for line in completed.stdout.decode('ascii', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        if JOB_ID.fullmatch(line) is None:
            raise SchedulerError('SCHEDULER_UNAVAILABLE', 'qselect returned a malformed job ID')
        if line not in ids:
            ids.append(line)
    ids.sort(key=lambda value: int(value.split('.', 1)[0].split('[', 1)[0]))
    return ids[-MAX_JOBS:], len(ids) > MAX_JOBS


def _row(job_id, record):
    state = record.get('job_state')
    state = state if isinstance(state, str) and len(state) == 1 else None
    requested = record.get('Resource_List') if isinstance(record.get('Resource_List'), dict) else {}
    used = record.get('resources_used') if isinstance(record.get('resources_used'), dict) else {}
    exit_status = record.get('Exit_status')
    return {'pbs_id': job_id, 'name': _text(record.get('Job_Name'), 256), 'state': state,
            'phase': PHASES.get(state, 'other'), 'substate': _scalar(record.get('substate')),
            'queue': _text(record.get('queue'), 64), 'server': _text(record.get('server'), 64),
            'requested': {key: _scalar(requested[key]) for key in REQUESTED if key in requested},
            'used': {key: _scalar(used[key]) for key in USED if key in used},
            'exit_status': exit_status if type(exit_status) is int else None,
            'comment': _text(record.get('comment'), 512),
            'queued_at': _text(record.get('qtime'), 64), 'started_at': _text(record.get('stime'), 64),
            'finished_at': _text(record.get('obittime'), 64), 'run_count': _scalar(record.get('run_count'))}


def pbs_status(qselect, qstat, user, *, run=subprocess.run):
    """The user's PBS jobs as scheduler-reported rows; values are verbatim, never inferred."""
    ids, truncated = own_job_ids(qselect, user, run=run)
    document = {}
    if ids:
        argv = [qstat, '-x', '-f', '-F', 'json', *ids]
        completed = _query(argv, run)
        try:
            document = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise (_failure(completed, argv) if completed.returncode else
                   SchedulerError('SCHEDULER_UNAVAILABLE', 'qstat JSON is malformed'))
        if not document and completed.returncode:
            raise _failure(completed, argv)
    jobs = document.get('Jobs', {})  # PBS omits the mapping when no job matched.
    if not isinstance(jobs, dict):
        raise SchedulerError('SCHEDULER_UNAVAILABLE', 'qstat JSON lacks a job mapping')
    rows, foreign = [], 0
    for job_id in ids:
        record = jobs.get(job_id)
        if not isinstance(record, dict):
            continue  # Left history between the two calls; nothing is inferred.
        owner = record.get('Job_Owner')
        if not isinstance(owner, str) or owner.split('@', 1)[0] != user:
            foreign += 1  # Never show another user's job (#81 S15).
            continue
        rows.append(_row(job_id, record))
    counts = {'queued': 0, 'running': 0, 'finished': 0, 'other': 0}
    for row in rows:
        counts[row['phase']] += 1
    reported = document.get('timestamp')
    return {'schema': 1, 'scheduler': 'pbs', 'pbs_version': _text(document.get('pbs_version'), 64),
            'servers': _text(document.get('pbs_server'), 128),
            'reported_at': reported if type(reported) is int else None,
            'jobs': rows, 'counts': counts, 'truncated': truncated, 'missing': len(ids) - len(rows) - foreign,
            'foreign_skipped': foreign,
            'qualification': 'Scheduler-reported, read-only; not CMW-managed and never adopted'}


class SchedulerBackend:
    """One PBS-backed queue per observer session: identity checks around read-only queries."""

    def __init__(self, config_root, queue_alias, *, clock=time.time, identity_loader=None,
                 identity_checker=None, boot_provider=None, run=subprocess.run, user=None, defer_check=False):
        from .identity import load_node, check_node
        if type(queue_alias) is not str or not queue_alias or len(queue_alias) > 128:
            raise SchedulerError('UNKNOWN_QUEUE', 'Select an explicitly enrolled queue alias')
        self.config_root, self.queue_alias = config_root, queue_alias
        self._load_identity = identity_loader or load_node
        self._check_identity = identity_checker or check_node
        self._custom_identity_checker = identity_checker is not None
        if boot_provider is None:
            from cmw.jobs.process_identity import boot_session_id
            boot_provider = boot_session_id
        self._boot_provider = boot_provider
        self._run = run
        self._user = user or pwd.getpwuid(os.getuid()).pw_name
        self.clock = clock
        self.session_id = uuid4().hex
        self.sequence = 0
        self.clock_id = None
        self._pinned = None
        self._runtime_verifier = None
        self._runtime_request = None
        self._closed = False
        self._lock = threading.Lock()
        if not defer_check:
            self._check()

    def _check(self, operation=None, *, phase='initial'):
        config = self._load_identity(self.config_root)
        if config.get('kind') != 'pbs':
            raise SchedulerError('INVALID_QUEUE', 'Node configuration is not a PBS-backed enrollment')
        if self._custom_identity_checker:
            readiness = self._check_identity(config)
        else:
            from .runtime_observation import RuntimeVerifier
            if self._runtime_verifier is None:
                self._runtime_verifier = RuntimeVerifier(config['runtime'], config.get('runtime_observation'))
            readiness = self._check_identity(config, operation=operation or 'capabilities',
                queue_alias=self.queue_alias, runtime_verifier=self._runtime_verifier,
                runtime_request=self._runtime_request, runtime_response=phase == 'after')
        if not readiness.get('identity_ready', False):
            raise SchedulerError('IDENTITY_CHANGED', '; '.join(readiness.get('identity_errors') or ['Node identity is unavailable']))
        if not readiness.get('runtime_ready', False):
            raise SchedulerError('RUNTIME_MISMATCH', '; '.join(readiness.get('runtime_errors') or ['Pinned endpoint runtime differs'])[:2048])
        queue = config['queues'].get(self.queue_alias)
        if not isinstance(queue, dict):
            raise SchedulerError('UNKNOWN_QUEUE', f'Queue is not enrolled: {self.queue_alias}')
        try:
            boot = self._boot_provider()
        except OSError as exc:
            raise SchedulerError('IDENTITY_CHANGED', 'Cannot verify endpoint boot session') from exc
        clock_id = _digest({'host': config['host_id'], 'boot': boot})
        pinned = (config['node_id'], queue['queue_id'], config['host_id'], config['runtime']['fingerprint'], clock_id)
        if self._pinned is not None and pinned != self._pinned:
            raise SchedulerError('IDENTITY_CHANGED', 'Endpoint identity changed during this observation session')
        self._pinned, self.clock_id = pinned, clock_id
        self.config, self.readiness, self.queue = config, readiness, queue
        return readiness

    def _status(self, params):
        if type(params) is not dict or set(params) - {'offset', 'limit'}:
            raise SchedulerError('INVALID_PARAMS', 'Operation parameters differ from the negotiated schema')
        if self.readiness.get('scheduler_ready') is not True:
            raise SchedulerError('NODE_NOT_READY', '; '.join(self.readiness.get('errors') or ['PBS clients are not ready']))
        tools = self.config['scheduler']
        pbs = pbs_status(tools['qselect']['path'], tools['qstat']['path'], self._user, run=self._run)
        controller = {'schema': 1, 'kind': 'pbs', 'online': False, 'dispatch': 'scheduler-owned'}
        return {'state': {'jobs': [], 'controller': controller, 'pbs': pbs},
                'queue_revision': _digest(controller), 'job_count': 0, 'status_counts': {},
                'offset': 0, 'has_more': False}

    def call(self, operation, params=None, target=None, *, request_id=None):
        """Execute a read-only operation; every other negotiated operation is refused."""
        if not self._lock.acquire(blocking=False):
            raise SchedulerError('BUSY', 'This bounded observation session already has an in-flight request')
        try:
            if self._closed:
                raise SchedulerError('CLOSED', 'Observation session has closed')
            if type(operation) is not str or operation not in PROTOCOL_OPERATIONS or operation not in OPERATIONS:
                raise SchedulerError('UNSUPPORTED_OPERATION', 'PBS-backed nodes support only read-only capabilities and status')
            if target is not None:
                raise SchedulerError('INVALID_TARGET', 'This operation does not accept an attempt target')
            params = {} if params is None else params
            self._runtime_request = {} if operation == 'status' else None
            self._check(operation, phase='before')
            if operation == 'capabilities':
                if params:
                    raise SchedulerError('INVALID_PARAMS', 'Operation parameters differ from the negotiated schema')
                result = {'queue': deepcopy(self.queue), 'readiness': deepcopy(self.readiness),
                          'operations': list(OPERATIONS), 'scheduler': deepcopy(CAPABILITY),
                          'mutations_supported': False, 'observer_session': self.session_id}
            else:
                result = self._status(params)
            self._check(operation, phase='after')
            self.sequence += 1
            envelope = {'schema': 1, 'node_id': self.config['node_id'], 'queue_id': self.queue['queue_id'],
                        'queue_alias': self.queue_alias, 'session_id': self.session_id, 'sequence': self.sequence,
                        'observed_at': self.clock(), 'clock_id': self.clock_id,
                        'runtime': deepcopy(self.readiness.get('runtime') or self.config['runtime']),
                        'host': deepcopy(self.readiness.get('host') or self.config['host']), 'result': result}
            return json.loads(json.dumps(envelope, ensure_ascii=True, allow_nan=False))
        finally:
            self._runtime_request = None
            self._lock.release()

    def close(self):
        self._closed = True
