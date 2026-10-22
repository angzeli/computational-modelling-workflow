"""Preview-bound desired actions with receipts atomic with target-local intent.

The endpoint writes intent. Existing controllers/workers retain all process
ownership and signalling. A durable start receipt cannot make OS spawn atomic;
request replay therefore never launches another controller.
"""
from __future__ import annotations

from contextlib import nullcontext
import re

from cmw.core.provenance import stable_hash
from cmw.jobs import runtime, suspension
from cmw.jobs.ownership import exclusive, lock_held, owner_alive
from cmw.jobs.store import PENDING, Store, validate_note
from .protocol import NodeError, identifier, target_identity

JOB_ACTIONS = frozenset({'hold', 'release', 'move', 'cancel', 'suspend', 'continue'})
QUEUE_ACTIONS = frozenset({'dispatch.pause', 'dispatch.resume', 'controller.start', 'controller.stop'})


def _require(condition, code, message):
    if not condition:
        raise NodeError(code, message)


def _action(params, target, *, preview=False):
    _require(isinstance(params, dict), 'INVALID_PARAMS', 'Control parameters must be an object')
    params = dict(params)
    params.pop('_endpoint', None)  # Reserved endpoint binding is checked by LocalBackend.
    allowed = {'action', 'position'} if preview else {
        'action', 'position', 'expected_clock_id', 'expected_revision', 'confirm', 'reason'}
    _require(not set(params) - allowed, 'INVALID_PARAMS', 'Unexpected control parameters')
    action = params.get('action')
    _require(isinstance(action, str) and action in JOB_ACTIONS | QUEUE_ACTIONS,
             'INVALID_PARAMS', 'An explicit supported desired action is required')
    if action in JOB_ACTIONS:
        target_identity(target)
    else:
        _require(target is None, 'INVALID_TARGET', 'Queue actions must not carry a job target')
    if action == 'move':
        _require(type(params.get('position')) is int and params['position'] >= 1,
                 'INVALID_PARAMS', 'Move requires a positive integer position')
    else:
        _require('position' not in params, 'INVALID_PARAMS', 'Only move accepts a position')
    return params


def normalize_request(target, params):
    params = _action(params, target)
    clock = params.get('expected_clock_id')
    _require(isinstance(clock, str) and re.fullmatch(r'[a-f0-9]{64}', clock),
             'INVALID_PARAMS', 'Expected clock identity must come from the original preview')
    revisions = params.get('expected_revision')
    _require(isinstance(revisions, dict) and set(revisions) == {'job', 'queue'},
             'INVALID_PARAMS', 'Expected job and queue revisions are required')
    for key in ('job', 'queue'):
        value = revisions[key]
        _require(value is None if key == 'job' and target is None else type(value) is int and value >= 0,
                 'INVALID_PARAMS', 'Expected revisions must be nonnegative counters (job null for queue actions)')
    _require(type(params.get('confirm', False)) is bool, 'INVALID_PARAMS', 'Confirmation must be boolean')
    return {**params, 'confirm': params.get('confirm', False),
            'reason': validate_note(params.get('reason', ''))}


def request_digest(node_id, queue_id, target, params):
    """Stable semantics shared with the frontend's durable request outbox."""
    identifier(node_id, 'node ID')
    identifier(queue_id, 'queue ID')
    normalized = normalize_request(target, params)
    if target is not None:
        _require((target['node_id'], target['queue_id']) == (node_id, queue_id),
                 'WRONG_TARGET', 'Control target belongs to another node or queue')
    return stable_hash({'node_id': node_id, 'queue_id': queue_id, 'target': target, 'params': normalized})


def _selected(backend, jobs, target):
    if target is None:
        return None
    target_identity(target)
    _require((target['node_id'], target['queue_id']) ==
             (backend.config['node_id'], backend.queue['queue_id']),
             'WRONG_TARGET', 'Control target belongs to another node or queue')
    job = next((item for item in jobs if item['id'] == target['job_id']), None)
    _require(job is not None and job['attempt_id'] == target['attempt_id'],
             'ATTEMPT_CHANGED', 'Selected immutable attempt differs; select the current target again')
    return job


def _revisions(control, job):
    return {'job': None if job is None else Store.node_revision(job),
            'queue': Store.node_revision(control)}


def _preview(backend, params, target):
    params = _action(params, target, preview=True)
    action = params['action']
    state = backend.store.snapshot()
    job = _selected(backend, state['jobs'], target)
    control = state['controller']
    reasons, consequences = [], []
    details = None
    if action in {'hold', 'release', 'move'}:
        if job['status'] not in PENDING:
            reasons.append('Only pending jobs can be held, released or reordered')
        if action == 'move' and params['position'] > sum(item['status'] in PENDING for item in state['jobs']):
            reasons.append('Requested position is outside the pending queue')
        if action == 'release':
            consequences.append('Release makes this job eligible; existing dispatch and admission rules apply immediately.')
    elif action == 'cancel':
        if job['status'] not in PENDING | {'Starting', 'Run', 'Cancelling', 'Cancelled'}:
            reasons.append('This status cannot be cancelled; no unverified process will be signalled')
        consequences.append('Cancel is permanent. Active cancellation is performed only by the existing owning worker.')
    elif action in {'suspend', 'continue'}:
        details = suspension.readiness(backend.store, job['id'], action)
        reasons.extend(details['reasons'])
        previous = (job.get('suspension') or {}).get('request') or {}
        if previous.get('action') == action and details['phase'] in (
                {'Suspending', 'Suspended'} if action == 'suspend' else {'Resuming', 'Run'}):
            reasons = [reason for reason in reasons if not reason.startswith(action.capitalize() + ' requires verified ')]
        consequences.append('Acceptance records desired state; completion requires fresh verification by the owning worker.')
    elif action == 'dispatch.resume':
        if any(item['status'] in {'Unknown', 'Cancelling'} for item in state['jobs']):
            reasons.append('Needs attention or cancellation is unresolved; dispatch remains paused')
        consequences.append('Dispatch may admit eligible queued jobs when this controller is online.')
    elif action == 'controller.start':
        if control['online'] or owner_alive(control.get('owner')) or lock_held(backend.store.root / 'controller.lock'):
            reasons.append('Controller is online or ownership is uncertain; inspect status')
        consequences.append('Start launches a detached controller and enables dispatch unless unresolved work blocks it. Held jobs remain held.')
    elif action == 'controller.stop':
        consequences.append('Stop pauses dispatch and stops the controller; active execution workers continue unchanged.')
    return {'action': action, 'target': target, 'node_id': backend.config['node_id'],
            'queue_id': backend.queue['queue_id'], 'expected_clock_id': backend.clock_id,
            'expected_revision': _revisions(control, job), 'ready': not reasons,
            'reasons': reasons, 'confirmation_required': action in {'cancel', 'suspend', 'continue'},
            'consequences': consequences, 'suspension': details,
            **({'position': params['position']} if 'position' in params else {})}


def _previous(record, digest):
    if record is None:
        return None
    _require(record['operation'] == 'control' and record['digest'] == digest,
             'REQUEST_CONFLICT', 'Request identity already names different semantics')
    return record['result']


def dispatch(backend, operation, params, *, target=None, request_id=None):
    """Endpoint adapter; backend verifies enrolled/pinned identity before dispatch."""
    if operation == 'control.preview':
        return _preview(backend, params, target)
    _require(operation == 'control', 'UNSUPPORTED_OPERATION', 'Unsupported control operation')
    identifier(request_id, 'request ID')
    params = normalize_request(target, params)
    digest = request_digest(backend.config['node_id'], backend.queue['queue_id'], target, params)
    store = backend.store
    previous = _previous(store.request_lookup(request_id), digest)
    if previous is not None:
        return previous
    action = params['action']
    preview_params = {key: params[key] for key in ('action', 'position') if key in params}
    preview = _preview(backend, preview_params, target)
    start_lock = exclusive(store.root / 'start.lock') if action == 'controller.start' else nullcontext()
    with start_lock:
        with store.transaction(create=False) as con:
            previous = _previous(store._request_record(con, request_id), digest)
            if previous is not None:
                return previous
            identity = store._queue_identity(con, store.control(con)['schema'])
            _require(identity is not None and all(identity[key] == backend.queue[key] for key in identity),
                     'QUEUE_CHANGED', 'Enrolled queue identity changed before control')
            job = _selected(backend, store.rows(con), target)
            _require(params['expected_clock_id'] == backend.clock_id, 'STALE_PREVIEW',
                     'Node boot/clock differs from the original confirmation; obtain a fresh preview')
            _require(params['expected_revision'] == _revisions(store.control(con), job),
                     'STALE_PREVIEW', 'Relevant state changed; obtain a fresh preview without reusing confirmation')
            _require(preview['ready'], 'CONTROL_UNAVAILABLE', '; '.join(preview['reasons']))
            _require(not preview['confirmation_required'] or params['confirm'],
                     'CONFIRMATION_REQUIRED', 'Explicit confirmation of this node, queue and attempt is required')
            status, result = 'COMPLETED', None
            if action in {'hold', 'release', 'move', 'cancel'}:
                if action == 'cancel' and job['status'] in {'Cancelling', 'Cancelled'}:
                    changed = job
                else:
                    changed = store._change(con, job['id'], action, position=params.get('position'), confirm=params['confirm'])
                status = 'ACCEPTED' if changed['status'] == 'Cancelling' else 'COMPLETED'
                result = {'status': changed['status'], 'order': changed['order']}
            elif action in {'suspend', 'continue'}:
                # Evidence may age while BEGIN IMMEDIATE waits for another writer.
                # Recheck ownership/freshness, preserving the user's original counters.
                readiness = suspension.readiness(store, job['id'], action)
                detail = suspension._submit(store, con, job['id'], action, readiness,
                                            reason=params['reason'], expected_attempt_id=target['attempt_id'])
                status = 'COMPLETED' if detail['result'] == 'COMPLETED' else 'ACCEPTED'
                current = store.get(con, job['id'])['suspension']
                result = {'phase': current['phase'], 'revision': current['revision'],
                          'worker_request_id': (current.get('request') or {}).get('id')}
            elif action.startswith('dispatch.'):
                store._dispatch(con, action == 'dispatch.resume')
            elif action == 'controller.start':
                _require(not owner_alive(store.control(con).get('owner'))
                         and not lock_held(store.root / 'controller.lock'),
                         'CONTROL_UNAVAILABLE', 'Controller ownership changed; inspect status')
                runtime._prepare_start(store, con)
                status = 'ACCEPTED'
            elif action == 'controller.stop':
                runtime._stop(store, con)
                status = 'ACCEPTED'
            # Even an already-satisfied desired action consumes its confirmation.
            # This also prevents a second start request racing controller startup.
            if Store.node_revision(store.control(con)) == params['expected_revision']['queue']:
                store.bump_control_revision(con)
            receipt = {'request_id': request_id, 'digest': digest, 'action': action,
                       'target': target, 'node_id': backend.config['node_id'], 'queue_id': backend.queue['queue_id'],
                       'status': status, 'expected_clock_id': params['expected_clock_id'],
                       'expected_revision': params['expected_revision'], 'result': result,
                       'recorded_revision': _revisions(store.control(con), None if job is None else store.get(con, job['id']))}
            if action == 'controller.start':
                receipt.update(launch_status='UNCONFIRMED', message='Startup intent recorded. Inspect current status; request lookup never relaunches. If still offline, obtain a fresh preview and a new explicit request.')
            elif status == 'ACCEPTED':
                receipt['message'] = 'Intent recorded; inspect current status for completion.'
            store.record_request(con, request_id, 'control', digest, receipt)
        if action == 'controller.start':
            # A crash anywhere after commit is reconciled by receipt/status lookup,
            # never by replaying spawn from the same request identity.
            try:
                runtime._launch_controller(store)
            except OSError:
                pass  # The durable receipt deliberately remains launch-unconfirmed.
    return receipt
