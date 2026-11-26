"""Read-only scheduler-backed nodes (issue #81 S07): one backend, one adapter per scheduler.

The scheduler, not CMW, owns admission, placement and dispatch on these nodes.
`SchedulerBackend` answers only ``capabilities`` and ``status``. Its status
carries a scheduler-neutral, schema-1 ``scheduler_report`` that the frontend
lane and portal read without knowing which scheduler produced it. The enrolled
``scheduler.kind`` selects the adapter that fills the report; PBS Pro is the only
one. Every site fact comes from the enrollment or from what the scheduler
reports. Nothing here submits, signals or adopts a job, or opens a queue database.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import threading
import time
from uuid import uuid4

from .contracts import SCHEDULER_POLL_BOUNDS, scheduler_poll_interval
from .protocol import NodeError, OPERATIONS as PROTOCOL_OPERATIONS
from .scheduler_report import PHASES, QUALIFICATION

OPERATIONS = ('capabilities', 'status')
DEFAULT_POLL_INTERVAL = SCHEDULER_POLL_BOUNDS[0]
# The endpoint outlives the frontend's three-interval staleness threshold by one
# interval, then exits: a lane that silent is already shown as stale.
IDLE_INTERVALS = 4
DEFAULT_IDLE_EXIT_SECONDS = IDLE_INTERVALS * DEFAULT_POLL_INTERVAL
MAX_JOBS = 1000
MAX_OUTPUT = 16 * 1024 * 1024
QUERY_TIMEOUT = 30
# Rows per status page stay well inside protocol.MAX_FRAME (2 MiB) with the envelope.
MAX_PAGE_BYTES = 1536 * 1024
# A job ID is an opaque token. It is checked only so that it can never be read
# as an option or carry whitespace or control bytes between the two calls.
JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@\[\]+-]{0,254}')
HELPER = Path(__file__).with_name('pbs_client.sh')
# PBS Pro job_state letters, grouped for counts. Rows keep the verbatim letter.
PBS_PHASES = {'Q': 'queued', 'H': 'queued', 'W': 'queued', 'T': 'queued', 'M': 'queued',
              'R': 'running', 'E': 'running', 'B': 'running', 'S': 'running', 'U': 'running',
              'F': 'finished', 'X': 'finished'}
REQUESTED = ('select', 'ncpus', 'mpiprocs', 'mem', 'walltime', 'nodect', 'place')
USED = ('walltime', 'cput', 'mem', 'ncpus', 'cpupercent')


def poll_interval(scheduler):
    """The enrolled poll interval of a scheduler configuration (S05 bounds)."""
    return scheduler_poll_interval(scheduler.get('poll_interval_seconds', DEFAULT_POLL_INTERVAL))


def idle_exit_seconds(config):
    return IDLE_INTERVALS * poll_interval(config['scheduler'])


class SchedulerError(NodeError):
    def __init__(self, code, message):
        super().__init__(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _text(value, limit):
    from cmw.jobs.cli import name_text  # One literal line: no control, format or line-break characters.
    return None if value is None else name_text(value)[:limit]


def _scalar(value, limit=64):
    if type(value) is int and abs(value) <= 2**53:
        return value
    return _text(value, limit) if isinstance(value, str) else None


def _integer(value):
    """An integer field some servers print as a decimal string; anything else is absent."""
    if isinstance(value, str) and re.fullmatch(r'-?[0-9]{1,15}', value.strip()):
        value = int(value)
    return value if type(value) is int and abs(value) <= 2**53 else None


def run_helper(argv, timeout):
    """Start the fixed shell helper in its own session; a timeout stops its whole group."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _query(client, arguments, run):
    """One read-only PBS client call through the shell helper; output is bounded there."""
    name = os.path.basename(client)
    argv = ['/bin/bash', str(HELPER), str(MAX_OUTPUT), client, *arguments]
    try:
        completed = run(argv, QUERY_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'{name} timed out') from exc
    except OSError as exc:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'{name} could not run: {exc}') from exc
    if len(completed.stdout) > MAX_OUTPUT:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'{name} output exceeds its bound')
    if completed.returncode == 125:
        raise SchedulerError('SCHEDULER_UNAVAILABLE', f'The PBS client helper refused {name}')
    return completed


def _failure(completed, client):
    reason = _text(completed.stderr[:4096].decode('utf-8', errors='replace').strip(), 512) or 'no diagnostic'
    return SchedulerError('SCHEDULER_UNAVAILABLE',
                          f'{os.path.basename(client)} exited {completed.returncode}: {reason}')


def own_job_ids(qselect, user, *, run=run_helper):
    """One user's current and history-retained job IDs in the order qselect lists them.

    IDs are opaque: neither their sequence part nor a server suffix is parsed.
    At most the last MAX_JOBS listed are kept.
    """
    completed = _query(qselect, ['-x', '-u', user], run)
    if completed.returncode != 0:
        raise _failure(completed, qselect)
    ids = []
    for line in completed.stdout.decode('ascii', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        if JOB_ID.fullmatch(line) is None:
            raise SchedulerError('SCHEDULER_UNAVAILABLE', 'qselect returned a malformed job ID')
        if line not in ids:
            ids.append(line)
    return ids[-MAX_JOBS:], len(ids) > MAX_JOBS


def _row(job_id, record):
    state = record.get('job_state')
    state = state if isinstance(state, str) and len(state) == 1 else None
    requested = record.get('Resource_List') if isinstance(record.get('Resource_List'), dict) else {}
    used = record.get('resources_used') if isinstance(record.get('resources_used'), dict) else {}
    return {'id': job_id, 'name': _text(record.get('Job_Name'), 256), 'state': state,
            'phase': PBS_PHASES.get(state, 'other'), 'substate': _scalar(record.get('substate')),
            'queue': _text(record.get('queue'), 64), 'server': _text(record.get('server'), 64),
            'requested': {key: _scalar(requested[key]) for key in REQUESTED if key in requested},
            'used': {key: _scalar(used[key]) for key in USED if key in used},
            'elapsed': _text(used.get('walltime'), 64) if isinstance(used.get('walltime'), str) else None,
            'exit_status': _integer(record.get('Exit_status')),
            'comment': _text(record.get('comment'), 512),
            'queued_at': _text(record.get('qtime'), 64), 'started_at': _text(record.get('stime'), 64),
            'finished_at': _text(record.get('obittime'), 64), 'run_count': _integer(record.get('run_count'))}


def report(kind, rows, *, version=None, servers=None, reported_at=None, truncated=False, missing=0,
           foreign_skipped=0):
    """The scheduler-neutral schema-1 report a lane reads; rows are newest first."""
    counts = dict.fromkeys(PHASES, 0)
    for row in rows:
        counts[row['phase']] += 1
    return {'schema': 1, 'kind': kind, 'version': version, 'servers': servers, 'reported_at': reported_at,
            'jobs': rows, 'counts': counts, 'truncated': truncated, 'missing': missing,
            'foreign_skipped': foreign_skipped, 'qualification': QUALIFICATION}


def pbs_status(qselect, qstat, user, *, run=run_helper):
    """The user's PBS jobs as a scheduler report, last listed first; values are verbatim.

    Two bounded calls (S05): `qselect -x -u` for the IDs, then `qstat -x -f -F
    json` for those IDs. The pair works on every server, including one where a
    single `qstat -f -F json -u` call would also have answered.
    """
    ids, truncated = own_job_ids(qselect, user, run=run)
    document = {}
    if ids:
        completed = _query(qstat, ['-x', '-f', '-F', 'json', *ids], run)
        try:
            document = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise (_failure(completed, qstat) if completed.returncode else
                   SchedulerError('SCHEDULER_UNAVAILABLE', 'qstat JSON is malformed'))
        if not document and completed.returncode:
            raise _failure(completed, qstat)
    jobs = document.get('Jobs', {})  # PBS omits the mapping when no job matched.
    if not isinstance(jobs, dict):
        raise SchedulerError('SCHEDULER_UNAVAILABLE', 'qstat JSON lacks a job mapping')
    rows, foreign = [], 0
    for job_id in reversed(ids):
        record = jobs.get(job_id)
        if not isinstance(record, dict):
            continue  # Left history between the two calls; nothing is inferred.
        owner = record.get('Job_Owner')
        if not isinstance(owner, str) or owner.split('@', 1)[0] != user:
            foreign += 1  # Never show another user's job, or one whose owner is unreported (#81 S15).
            continue
        rows.append(_row(job_id, record))
    return report('pbs', rows, version=_text(document.get('pbs_version'), 64),
                  servers=_text(document.get('pbs_server'), 128), reported_at=_integer(document.get('timestamp')),
                  truncated=truncated, missing=len(ids) - len(rows) - foreign, foreign_skipped=foreign)


def _pbs(scheduler, user, run):
    return pbs_status(scheduler['qselect']['path'], scheduler['qstat']['path'], user, run=run)


# The S07 adapter boundary: enrolled scheduler kind -> report(scheduler config, user, run).
ADAPTERS = {'pbs': _pbs}


def page(rows, offset, limit):
    """One page of rows that fits the frame budget; a short page reports where to resume."""
    selected = rows[offset:offset + limit]
    size = 2  # The enclosing JSON array.
    for index, row in enumerate(selected):
        size += len(json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':'))) + 1
        if size > MAX_PAGE_BYTES:
            selected = selected[:index]
            break
    end = offset + len(selected)
    return {'jobs': selected, 'total': len(rows), 'offset': offset,
            'next_offset': end if end < len(rows) else None,
            'size_limited': len(selected) < len(rows[offset:offset + limit])}


class SchedulerBackend:
    """One scheduler-backed queue per observer session: identity checks around read-only queries."""

    def __init__(self, config_root, queue_alias, *, clock=time.time, identity_loader=None,
                 identity_checker=None, boot_provider=None, run=run_helper, user=None, defer_check=False):
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
        if config.get('kind') != 'pbs' or config.get('scheduler', {}).get('kind') not in ADAPTERS:
            raise SchedulerError('INVALID_QUEUE', 'Node configuration is not a scheduler-backed enrollment')
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
        offset, limit = params.get('offset', 0), params.get('limit', MAX_JOBS)
        if type(offset) is not int or not 0 <= offset <= MAX_JOBS:
            raise SchedulerError('INVALID_PARAMS', f'offset must be an integer between 0 and {MAX_JOBS}')
        if type(limit) is not int or not 1 <= limit <= MAX_JOBS:
            raise SchedulerError('INVALID_PARAMS', f'limit must be an integer between 1 and {MAX_JOBS}')
        if self.readiness.get('scheduler_ready') is not True:
            raise SchedulerError('NODE_NOT_READY', '; '.join(self.readiness.get('errors') or ['Scheduler clients are not ready']))
        scheduler = self.config['scheduler']
        observed = ADAPTERS[scheduler['kind']](scheduler, self._user, self._run)
        observed.update(page(observed.pop('jobs'), offset, limit))
        controller = {'schema': 1, 'kind': 'scheduler', 'online': False, 'dispatch': 'scheduler-owned'}
        return {'state': {'jobs': [], 'controller': controller, 'scheduler_report': observed},
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
                raise SchedulerError('UNSUPPORTED_OPERATION', 'Scheduler-backed nodes support only read-only capabilities and status')
            if target is not None:
                raise SchedulerError('INVALID_TARGET', 'This operation does not accept an attempt target')
            params = {} if params is None else params
            self._runtime_request = {} if operation == 'status' else None
            self._check(operation, phase='before')
            if operation == 'capabilities':
                if params:
                    raise SchedulerError('INVALID_PARAMS', 'Operation parameters differ from the negotiated schema')
                scheduler = self.config['scheduler']
                capability = {'schema': 1, 'kind': scheduler['kind'], 'read_only': True,
                              'poll_interval_seconds': poll_interval(scheduler),
                              'idle_exit_seconds': idle_exit_seconds(self.config)}
                result = {'queue': deepcopy(self.queue), 'readiness': deepcopy(self.readiness),
                          'operations': list(OPERATIONS), 'scheduler': capability,
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
