"""Small node-local observation boundary shared by endpoint and frontend clients.

All filesystem, process and scientific interpretation stays on this node. The
backend is an observer session, never an execution lease or a Store substitute.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from uuid import uuid4

from cmw.jobs.store import JobsError, Store
from cmw.jobs.locations import _read_job, discover
from cmw.jobs.progress import resolve_context
from cmw.jobs.source_selection import inspect_selection, validate_selection, activate_selection
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from .codec import encode_typed, decode_typed
from .protocol import NodeError, target_identity, MUTATIONS, identifier


OPERATIONS = frozenset({'capabilities', 'status', 'lookup', 'show', 'observe',
    'progress', 'history', 'history.release', 'logs', 'locations', 'sources.inspect',
    'sources.validate', 'sources.activate', 'sources.clear', 'request', 'submit.preview',
    'submit', 'transfer.begin', 'transfer.publish', 'transfer.lookup'})
MAX_ROWS = 1000
MAX_LOG_BYTES = 65536


class BackendError(NodeError):
    def __init__(self, code, message):
        super().__init__(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def job_revision(job):
    """Revision of control-relevant persisted fields; observation clocks do not churn it."""
    keys = ('id', 'attempt_id', 'status', 'order', 'claim', 'claimed_at', 'worker',
            'group', 'started_at', 'finished_at', 'cancel_requested', 'termination_requested',
            'scheduling', 'sharing_anchor', 'note_revision')
    record = {key: job.get(key) for key in keys}
    suspension = job.get('suspension') or {}
    record['suspension'] = {key: suspension.get(key) for key in
        ('backend', 'protocol', 'phase', 'desired', 'revision', 'request', 'inhibited')}
    return _digest(record)


def queue_revision(control):
    return _digest({key: control.get(key) for key in ('schema', 'dispatch', 'stop', 'owner', 'scheduler')})


def validate_target(target):
    return deepcopy(target_identity(target))


def _params(params, allowed=(), required=()):
    if type(params) is not dict or set(params) - set(allowed) or set(required) - set(params):
        raise BackendError('INVALID_PARAMS', 'Operation parameters differ from the negotiated schema')


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BackendError('INVALID_PARAMS', f'{name} must be an integer between {minimum} and {maximum}')
    return value


def _log_tail(path, limit):
    """A bounded regular-file read; never wait on a FIFO named like an operational log."""
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {'status': 'NOT_CREATED', 'text': '', 'bytes': 0, 'changing': False}
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise BackendError('LOG_UNAVAILABLE', 'Declared operational log is not a regular file')
        handle.seek(max(0, before.st_size - limit))
        raw = handle.read(limit)
        after = os.fstat(handle.fileno())
    from cmw.jobs.cli import safe_text
    return {'status': 'OBSERVATIONAL', 'text': safe_text(raw.decode('utf-8', errors='replace')),
            'bytes': len(raw), 'changing': (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns),
            'qualification': 'Bounded node-local log capture; not a stable archive or execution proof'}


class LocalBackend:
    """One enrolled queue and bounded node-local observer resources per session."""

    def __init__(self, config_root, queue_alias, *, clock=time.time,
                 identity_loader=None, identity_checker=None, observer=None,
                 activity_projector=None, boot_provider=None):
        from .identity import load_node, check_node
        if type(queue_alias) is not str or not queue_alias or len(queue_alias) > 128:
            raise BackendError('UNKNOWN_QUEUE', 'Select an explicitly enrolled queue alias')
        self.config_root, self.queue_alias = Path(config_root), queue_alias
        self._load_identity = identity_loader or load_node
        self._check_identity = identity_checker or check_node
        self.clock = clock
        self.session_id = uuid4().hex
        import psutil
        self._boot_provider = boot_provider or psutil.boot_time
        self.clock_id = None
        self.sequence = 0
        self._closed = False
        self._lock = threading.Lock()
        self._pinned = None
        self._sessions = OrderedDict()
        self._validations = OrderedDict()
        self._generation = 0
        self.observer = observer or ObservationService(clock=clock, max_contexts=4, max_inflight=1)
        self._activity_projector = activity_projector
        self._activity_observer = None
        self._sampler = None
        self._check()

    def _check(self):
        config = self._load_identity(self.config_root)
        readiness = self._check_identity(config)
        if not readiness.get('identity_ready', False):
            raise BackendError('IDENTITY_CHANGED', '; '.join(readiness.get('identity_errors') or readiness.get('errors') or ['Node/queue identity is unavailable']))
        if not readiness.get('runtime_ready', False):
            raise BackendError('RUNTIME_MISMATCH', '; '.join(readiness.get('runtime_errors') or ['Pinned endpoint runtime differs']))
        record = config.get('queues', {}).get(self.queue_alias)
        if not isinstance(record, dict):
            raise BackendError('UNKNOWN_QUEUE', f'Queue is not enrolled: {self.queue_alias}')
        state = record.get('state') or record.get('state_root')
        if not isinstance(state, str):
            raise BackendError('INVALID_QUEUE', 'Enrolled queue lacks its local state path')
        store = Store(state)
        actual = store.queue_identity()
        if actual is None or any(actual.get(key) != record.get(key) for key in actual):
            raise BackendError('QUEUE_CHANGED', 'Enrolled queue identity differs from its authoritative database')
        if actual['node_id'] != config['node_id'] or actual['host_id'] != config['host_id']:
            raise BackendError('QUEUE_CHANGED', 'Queue belongs to a different node or host')
        clock_id = _digest({'host': config['host_id'], 'boot': self._boot_provider()})
        pinned = (config['node_id'], actual['queue_id'], config['host_id'], config['runtime']['fingerprint'], clock_id)
        if self._pinned is not None and pinned != self._pinned:
            raise BackendError('IDENTITY_CHANGED', 'Endpoint identity changed during this observation session')
        self._pinned = pinned
        self.clock_id = clock_id
        self.config, self.readiness, self.store, self.queue = config, readiness, store, actual
        return readiness

    def target(self, job):
        return {'node_id': self.config['node_id'], 'queue_id': self.queue['queue_id'],
                'job_id': job['id'], 'attempt_id': job['attempt_id']}

    def _job(self, target):
        target = validate_target(target)
        if (target['node_id'], target['queue_id']) != self._pinned[:2]:
            raise BackendError('WRONG_TARGET', 'Selected target belongs to another node or queue')
        job, _ = _read_job(self.store, target['job_id'])
        if job['attempt_id'] != target['attempt_id']:
            raise BackendError('ATTEMPT_CHANGED', 'Selected immutable attempt differs; select the current target again')
        return job

    def _row(self, job):
        return {**deepcopy(job), 'target': self.target(job), 'revision': job_revision(job)}

    def _key(self, job):
        return self.config['node_id'], self.queue['queue_id'], job['id'], job['attempt_id']

    def _context(self, job):
        key = self._key(job)
        session = self._sessions.get(key)
        try:
            context = resolve_context(self.store, job['id'], expected_attempt_id=job['attempt_id'],
                                      session_selection=session)
        except JobsError:
            if session is not None:
                self._sessions.pop(key, None)
                raise BackendError('SOURCE_SESSION_INVALID', 'Session sources changed; validate again on the execution node') from None
            raise
        self.observer.retain((context,))
        return context

    def _status(self, params):
        _params(params, ('offset', 'limit'))
        offset = _integer(params.get('offset', 0), 'offset', 0, 1000000000)
        limit = _integer(params.get('limit', MAX_ROWS), 'limit', 1, MAX_ROWS)
        if self._activity_projector is None:
            from cmw.jobs.activity import Observer, project
            from cmw.jobs.telemetry import Sampler
            if self._activity_observer is None:
                self._activity_observer = Observer()
                self._sampler = Sampler()
            state = project(self.store, observer=self._activity_observer, sampler=self._sampler)
        else:
            state = self._activity_projector(self.store)
        jobs = state['jobs']
        total = len(jobs)
        state['jobs'] = [self._row(job) for job in jobs[offset:offset + limit]]
        return {'state': state, 'queue_revision': queue_revision(state['controller']),
                'job_count': total, 'offset': offset, 'has_more': offset + limit < total}

    def _locations(self, job):
        found = discover(self.store, job['id'], expected_attempt_id=job['attempt_id'])
        result = []
        for location in found.locations:
            candidates = []
            if location.path:
                for alias, storage in self.config.get('storages', {}).items():
                    try:
                        relative = Path(location.path).relative_to(storage['root']).as_posix()
                    except ValueError:
                        continue
                    candidates.append((len(storage['root']), alias, storage, relative))
            _, alias, storage, relative = max(candidates, key=lambda value: value[0]) if candidates else (0, None, {}, None)
            storage_ready = self.readiness.get('storages', {}).get(alias, {}).get('ready') if alias else None
            result.append({'node_id': self.config['node_id'], 'queue_id': self.queue['queue_id'],
                'job_id': job['id'], 'attempt_id': job['attempt_id'], 'location_id': location.key,
                'storage_id': storage.get('storage_id'), 'registered_root': storage.get('root'),
                'relative_path': relative, 'node_path': location.path, 'declared_path': location.declared_path,
                'role': location.role, 'kind': location.kind, 'stage': location.stage,
                'origin': location.origin, 'accessibility': ('STORAGE_UNAVAILABLE' if alias and storage_ready is not True else location.accessibility),
                'storage_ready': storage_ready,
                'qualification': location.qualification, 'detail': location.detail,
                'scientific_validation': 'not-assessed', 'local_open_permitted': False})
        return {'target': self.target(job), 'status': found.status, 'locations': result,
                'preferred_id': found.preferred_key, 'diagnostics': list(found.diagnostics)}

    def _sources(self, operation, params, job):
        key = self._key(job)
        options = {'expected_attempt_id': job['attempt_id']}
        if operation == 'sources.inspect':
            _params(params)
            value = inspect_selection(self.store, job['id'], session_selection=self._sessions.get(key), **options)
            return {'inspection': encode_typed(value), 'session_active': key in self._sessions}
        if operation == 'sources.validate':
            _params(params, ('sources',), ('sources',))
            if type(params['sources']) is not list or not 1 <= len(params['sources']) <= 8:
                raise BackendError('INVALID_PARAMS', 'Validate one to eight typed source descriptors')
            sources = tuple(decode_typed(value, SourceDescriptor) for value in params['sources'])
            value = validate_selection(self.store, job['id'], sources, **options)
            token = uuid4().hex
            self._validations[token] = (key, value)
            while len(self._validations) > 16:
                self._validations.popitem(last=False)
            return {'validation_id': token, 'validation': encode_typed(value)}
        if operation == 'sources.activate':
            _params(params, ('validation_id', 'confirmed'), ('validation_id', 'confirmed'))
            if type(params['validation_id']) is not str or type(params['confirmed']) is not bool:
                raise BackendError('INVALID_PARAMS', 'Source activation requires its validation token and explicit confirmation')
            saved = self._validations.get(params['validation_id'])
            if saved is None or saved[0] != key:
                raise BackendError('SOURCE_SESSION_INVALID', 'Validation belongs to another target or endpoint session')
            value = activate_selection(self.store, saved[1], self._generation + 1, confirmed=params['confirmed'])
            self._generation += 1
            self._sessions[key] = value
            while len(self._sessions) > 32:
                self._sessions.popitem(last=False)
            del self._validations[params['validation_id']]
            self.observer.retain(())
            return {'selection': encode_typed(value)}
        _params(params)
        self._sessions.pop(key, None)
        self.observer.retain(())
        return {'cleared': True}

    def _selected(self, operation, params, job):
        if operation == 'show':
            _params(params)
            return {'job': self._row(job)}
        if operation == 'observe':
            _params(params)
            from cmw.jobs.lifecycle import observe_execution
            return {'execution': observe_execution(self.store, job, now=self.clock()).to_dict(now=self.clock())}
        if operation == 'locations':
            _params(params)
            return self._locations(job)
        if operation == 'logs':
            _params(params, ('stream', 'bytes'))
            stream = params.get('stream', 'stdout')
            if stream not in ('stdout', 'stderr'):
                raise BackendError('INVALID_PARAMS', 'Log stream must be stdout or stderr')
            limit = _integer(params.get('bytes', 16384), 'bytes', 1, MAX_LOG_BYTES)
            path = job.get('logs', {}).get(stream)
            if not path:
                raise BackendError('LOG_UNAVAILABLE', 'No declared log for this attempt')
            return {'target': self.target(job), 'stream': stream, 'node_path': path, **_log_tail(path, limit)}
        if operation.startswith('sources.'):
            return self._sources(operation, params, job)
        context = self._context(job)
        if operation == 'history.release':
            _params(params)
            self.observer.release_history(context)
            return {'released': True}
        if operation == 'progress':
            _params(params)
            return {'snapshot': encode_typed(self.observer.poll(context))}
        _params(params, ('cursor', 'limit', 'parent_id', 'stage', 'segment'))
        options = dict(params)
        options['limit'] = _integer(options.get('limit', 50), 'limit', 1, 200)
        for name in ('cursor', 'parent_id', 'stage'):
            if options.get(name) is not None and (type(options[name]) is not str or len(options[name]) > 16384):
                raise BackendError('INVALID_PARAMS', f'History {name} must be bounded text or null')
        if options.get('segment') is not None:
            _integer(options['segment'], 'segment', 1, 1000000000)
        page = self.observer.history_page(context, **options)
        snapshot = self.observer.snapshot(context)
        return {'page': encode_typed(page), 'snapshot': encode_typed(snapshot) if snapshot is not None else None}

    def call(self, operation, params=None, target=None, *, request_id=None):
        """Execute a known read-only operation against enrolled node-local services."""
        if not self._lock.acquire(blocking=False):
            raise BackendError('BUSY', 'This bounded observation session already has an in-flight request')
        try:
            if self._closed:
                raise BackendError('CLOSED', 'Observation session has closed')
            if type(operation) is not str or operation not in OPERATIONS:
                raise BackendError('UNSUPPORTED_OPERATION', 'Operation is not supported by this observation backend')
            params = {} if params is None else params
            if type(params) is not dict:
                raise BackendError('INVALID_PARAMS', 'Operation parameters must be an object')
            self._check()
            if operation in MUTATIONS:
                identifier(request_id, 'request ID')
                expected = {'node_id': self.config['node_id'], 'queue_id': self.queue['queue_id'],
                            'host_id': self.config['host_id'], 'runtime_id': self.config['runtime']['fingerprint'],
                            'clock_id': self.clock_id}
                if params.get('_endpoint') != expected:
                    raise BackendError('ENDPOINT_CHANGED', 'Mutation lacks the exact verified endpoint/boot guard')
                params = {key: value for key, value in params.items() if key != '_endpoint'}
            if operation in {'request', 'submit.preview', 'submit', 'transfer.begin', 'transfer.publish', 'transfer.lookup'}:
                if target is not None:
                    raise BackendError('INVALID_TARGET', 'Submission/transfer is not a mutation of an existing attempt')
                from .submission import dispatch
                result = dispatch(self, operation, params, request_id=request_id)
            elif operation in {'status', 'capabilities', 'lookup'}:
                if target is not None:
                    raise BackendError('INVALID_TARGET', 'This operation does not accept an attempt target')
                if operation == 'capabilities':
                    _params(params)
                    result = {'queue': deepcopy(self.queue), 'readiness': deepcopy(self.readiness),
                              'profiles': deepcopy(self.config.get('profiles', {})), 'operations': sorted(OPERATIONS),
                              'mutations_supported': True, 'observer_session': self.session_id}
                elif operation == 'status':
                    result = self._status(params)
                else:
                    _params(params, ('id',), ('id',))
                    if type(params['id']) not in (str, int) or len(str(params['id'])) > 128:
                        raise BackendError('INVALID_PARAMS', 'Lookup requires a bounded local job identifier')
                    job, _ = _read_job(self.store, params['id'])
                    result = {'target': self.target(job), 'job': self._row(job)}
            else:
                job = self._job(target)
                result = self._selected(operation, params, job)
                self._job(target)
            self._check()
            self.sequence += 1
            envelope = {'schema': 1, 'node_id': self.config['node_id'], 'queue_id': self.queue['queue_id'],
                    'queue_alias': self.queue_alias, 'session_id': self.session_id, 'sequence': self.sequence,
                    'observed_at': self.clock(), 'clock_id': self.clock_id,
                    'runtime': deepcopy(self.readiness.get('runtime', self.config['runtime'])),
                    'host': deepcopy(self.readiness.get('host', self.config['host'])), 'result': result}
            return json.loads(json.dumps(envelope, ensure_ascii=True, allow_nan=False))
        finally:
            self._lock.release()

    def close(self):
        self._closed = True
        self.observer.close()
        self._sessions.clear()
        self._validations.clear()
