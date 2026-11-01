"""Strict owner-node successor routes; no frontend path interpretation or copying."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from cmw.jobs.successor_models import SuccessorResult
from .codec import decode_typed, encode_typed
from .protocol import NodeError

CAPABILITY = {'schema': 1, 'model': 'SuccessorResult', 'geometry_only': True}
OPERATIONS = {'successor.plan': 'plan', 'successor.materialize': 'materialize', 'successor.register': 'register'}


def _path(value):
    if (type(value) is not str or not value or len(value) > 4096 or '\0' in value
            or not PurePosixPath(value).is_absolute()):
        raise NodeError('INVALID_PARAMS', 'Successor paths must be explicit absolute paths on the owning node')
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise NodeError('INVALID_PARAMS', 'Successor approval requires the exact reviewed SHA-256 plan digest')
    return value


def dispatch(backend, operation, params, target, *, request_id=None):
    from cmw.jobs.successor import SuccessorService
    from .backend import _params
    if operation not in OPERATIONS:
        raise NodeError('UNSUPPORTED_OPERATION', 'Unknown successor operation')
    action = OPERATIONS[operation]
    required = ('record_path', 'approved_digest') if action == 'register' else (
        ('spec_path',) if action == 'plan' else ('spec_path', 'approved_digest'))
    _params(params, required, required)
    path = _path(params['record_path' if action == 'register' else 'spec_path'])
    if action != 'plan':
        _digest(params['approved_digest'])
    job = backend._job(target)
    service = SuccessorService(backend.store, origin={
        'node_id': backend.config['node_id'], 'queue_id': backend.queue['queue_id'],
        'host_id': backend.config['host_id'], 'runtime_id': backend.config['runtime']['fingerprint']})
    expected = {'expected_attempt_id': job['attempt_id']}
    if action == 'plan':
        result = service.plan(job['id'], path, **expected)
    elif action == 'materialize':
        result = service.materialize(job['id'], path, params['approved_digest'], **expected)
    else:
        result = service.register(job['id'], path, params['approved_digest'], request_id, **expected)
    backend._job(target)
    if any(getattr(result, key) != value for key, value in target.items()) or result.operation != action:
        raise NodeError('INVALID_RESPONSE', 'Successor service result differs from the selected immutable parent or operation')
    return {'successor': encode_typed(result)}


def validate_result(envelope, operation, target=None):
    try:
        if operation not in OPERATIONS or set(envelope['result']) != {'successor'}:
            raise ValueError('Unexpected successor response fields')
        result = decode_typed(envelope['result']['successor'], SuccessorResult)
        if ((result.node_id, result.queue_id) != (envelope['node_id'], envelope['queue_id'])
                or result.operation != OPERATIONS[operation]):
            raise ValueError('Successor response belongs to another owner or operation')
        if target is not None and any(getattr(result, key) != value for key, value in target.items()):
            raise ValueError('Successor response differs from the selected immutable parent')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NodeError('INVALID_RESPONSE', 'Malformed or misbound successor response') from exc
