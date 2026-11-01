"""Negotiated owner-local convergence-study operations; never remote file copying."""
from __future__ import annotations

import json
from cmw.jobs.study_models import StudyResult
from .codec import decode_typed, encode_typed
from .protocol import NodeError
from .successor import _path as _absolute_path, _digest as _approval_digest

CAPABILITY = {'schema': 1, 'model': 'StudyResult', 'engine': 'VASP', 'max_cases': 64}
OPERATIONS = {'study.'+name: name for name in ('plan', 'materialize', 'register', 'compare', 'view')}


def _path(value):
    try:
        return _absolute_path(value)
    except NodeError as exc:
        raise NodeError('INVALID_PARAMS', 'Study paths must be explicit absolute paths on the owning node') from exc


def _digest(value):
    try:
        return _approval_digest(value)
    except NodeError as exc:
        raise NodeError('INVALID_PARAMS', 'Study approval requires the exact reviewed SHA-256 plan digest') from exc


def dispatch(backend, operation, params, target, *, request_id=None):
    from cmw.jobs.study import StudyService
    from .backend import _params
    if operation not in OPERATIONS:
        raise NodeError('UNSUPPORTED_OPERATION', 'Unknown study operation')
    action = OPERATIONS[operation]
    service = StudyService(backend.store, origin={
        'node_id': backend.config['node_id'], 'queue_id': backend.queue['queue_id'],
        'host_id': backend.config['host_id'], 'runtime_id': backend.config['runtime']['fingerprint']})
    if target is not None:
        if action != 'view':
            raise NodeError('INVALID_TARGET', 'Study planning and publication are queue scoped')
        _params(params)
        job = backend._job(target)
        result = service.view_for_job(job['id'], expected_attempt_id=job['attempt_id'])
        backend._job(target)
        if json.loads(result.payload_json).get('selected_target') != target:
            raise NodeError('INVALID_RESPONSE', 'Study view differs from the selected immutable attempt')
    elif action == 'plan':
        _params(params, ('manifest_path',), ('manifest_path',))
        result = service.plan(_path(params['manifest_path']))
    elif action == 'materialize':
        _params(params, ('manifest_path', 'approved_digest'), ('manifest_path', 'approved_digest'))
        result = service.materialize(_path(params['manifest_path']), _digest(params['approved_digest']))
    elif action == 'register':
        _params(params, ('record_path', 'approved_digest'), ('record_path', 'approved_digest'))
        result = service.register(_path(params['record_path']), _digest(params['approved_digest']), request_id)
    elif action == 'compare':
        _params(params, ('record_path', 'selection_path', 'export_directory'), ('record_path', 'selection_path'))
        result = service.compare(_path(params['record_path']), _path(params['selection_path']),
            export_directory=_path(params['export_directory']) if params.get('export_directory') is not None else None)
    else:
        _params(params, ('record_path',), ('record_path',))
        result = service.view(_path(params['record_path']))
    if (result.node_id, result.queue_id, result.operation) != (backend.config['node_id'], backend.queue['queue_id'], action):
        raise NodeError('INVALID_RESPONSE', 'Study result differs from the selected owner or operation')
    if action in {'materialize', 'register'} and result.plan_digest != params['approved_digest']:
        raise NodeError('INVALID_RESPONSE', 'Study publication response differs from the approved digest')
    if action == 'register' and result.study_id != request_id:
        raise NodeError('INVALID_RESPONSE', 'Study registration response differs from its durable study identity')
    return {'study': encode_typed(result)}


def validate_result(envelope, operation, target=None, *, approved_digest=None, request_id=None):
    try:
        if operation not in OPERATIONS or set(envelope['result']) != {'study'}:
            raise ValueError('Unexpected study response fields')
        result = decode_typed(envelope['result']['study'], StudyResult)
        if ((result.node_id, result.queue_id) != (envelope['node_id'], envelope['queue_id'])
                or result.operation != OPERATIONS[operation]):
            raise ValueError('Study response belongs to another owner or operation')
        if approved_digest is not None and result.plan_digest != approved_digest:
            raise ValueError('Study publication differs from the approved digest')
        if operation == 'study.register' and request_id is not None and result.study_id != request_id:
            raise ValueError('Study registration differs from its durable identity')
        if target is not None and (operation != 'study.view' or json.loads(result.payload_json).get('selected_target') != target):
            raise ValueError('Study view differs from the selected immutable attempt')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NodeError('INVALID_RESPONSE', 'Malformed or misbound study response') from exc
