"""Durable reviewed submission intent; transfers never authorize execution."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cmw.core.provenance import stable_hash
from .connections import _private, default_config
from .protocol import NodeError, decode, encode, identifier
from .submission import normalize_spec, submission_digest
from .transfer import prepare_local_bundle, upload_local_bundle, upload_sftp


def _record_path(root, request_id):
    root = Path(root) if root is not None else default_config()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(root, directory=True)
    requests = root / 'requests'
    requests.mkdir(mode=0o700, exist_ok=True)
    _private(requests, directory=True)
    return requests / identifier(request_id, 'request ID')


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(encode(value))
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare_submission(backend, specification, *, root=None):
    """Persist ID, immutable local spool and semantic digest before any mutation.

    The frontend document selects individual source files. Only its normalized
    manifest and portable references cross the endpoint boundary.
    """
    if not isinstance(specification, dict) or 'manifest' in specification or 'files' not in specification:
        raise NodeError('INVALID_SUBMISSION', 'Frontend specification requires explicit files selections, not a supplied manifest')
    identifier(specification.get('request_id'), 'request ID')
    if specification.get('node_id') != backend.node_id or specification.get('queue_id') != backend.queue_id:
        raise NodeError('WRONG_NODE', 'Submission differs from the explicitly selected node and queue')
    local_digest = stable_hash(specification)
    directory = _record_path(root, specification['request_id'])
    if directory.exists():
        _private(directory, directory=True)
        record_path = directory / 'request.json'
        if not record_path.exists():
            raise NodeError('INCOMPLETE_CLIENT_PREPARATION', 'Local preparation was interrupted before sending; retain evidence and use a new reviewed request ID')
        _private(record_path)
        with record_path.open('rb') as handle:
            record = decode(handle.read(256 * 1024 + 1))
        if (set(record) != {'schema', 'local_digest', 'digest', 'spec'} or record['schema'] != 1
                or record['local_digest'] != local_digest or record['digest'] != submission_digest(record['spec'])
                or record['spec']['request_id'] != specification['request_id']):
            raise NodeError('REQUEST_CONFLICT', 'Persisted request ID belongs to a different reviewed specification')
        return record, directory / 'spool'
    directory.mkdir(mode=0o700)
    portable = {key: value for key, value in specification.items() if key not in {'files', 'potcar_authorized'}}
    prepared = prepare_local_bundle(directory / 'spool', specification['files'], node_id=backend.node_id,
                                    queue_id=backend.queue_id, potcar_authorized=specification.get('potcar_authorized', False))
    portable['manifest'] = prepared['manifest']
    portable = normalize_spec(portable)
    record = {'schema': 1, 'local_digest': local_digest, 'digest': submission_digest(portable), 'spec': portable}
    _write_new(directory / 'request.json', record)
    return record, directory / 'spool'


def submit(backend, specification, *, root=None, preview=False):
    record, spool = prepare_submission(backend, specification, root=root)
    spec = record['spec']
    request_id = spec['request_id']
    if not preview:
        previous = backend.call('request', {'request_id': request_id})
        prior = previous['result']['request']
        if prior is not None:
            if prior['operation'] != 'submit' or prior['digest'] != record['digest']:
                raise NodeError('REQUEST_CONFLICT', 'Endpoint recorded different semantics for this request identity')
            return {**previous, 'result': {'status': 'REGISTERED', 'replayed': True,
                                         'request_id': request_id, 'job': prior['result']}}
    planned = backend.call('submit.preview', {'spec': spec})
    if preview:
        dispatch = backend.call('status')['result']['state']['controller']['dispatch']
        planned['result']['dispatch_intent'] = dispatch
        planned['result']['client_request_record'] = str(spool.parent / 'request.json')
        return planned
    transfer = backend.call('transfer.begin', {'storage': spec['storage'], 'request_id': request_id,
                                              'manifest': spec['manifest']})['result']
    if transfer['status'] != 'PUBLISHED':
        if backend.connection['transport'] == 'ssh':
            upload_sftp(backend.connection, spool, transfer)
        else:
            upload_local_bundle(spool, transfer)
        backend.call('transfer.publish', {'storage': spec['storage'], 'request_id': request_id,
                                         'manifest_digest': transfer['manifest_digest']})
    # Lost reply is deliberately not an automatic new request. The durable ID
    # above supports explicit lookup/retry, including after client termination.
    return backend.call('submit', {'spec': spec}, request_id=request_id)


def _operation_record(root, request_id, operation, target, params, digest):
    """A retry retains the original reviewed target, clock and revisions."""
    directory = _record_path(root, request_id)
    value = {'schema': 1, 'request_id': request_id, 'operation': operation,
             'target': target, 'params': params, 'digest': digest}
    if directory.exists():
        _private(directory, directory=True)
        path = directory / 'request.json'
        _private(path)
        with path.open('rb') as handle:
            previous = decode(handle.read(256 * 1024 + 1))
        if previous != value:
            raise NodeError('REQUEST_CONFLICT', 'This client request ID has different persisted intent; inspect the original request')
    else:
        directory.mkdir(mode=0o700)
        _write_new(directory / 'request.json', value)
    return value


def apply_control(backend, preview_result, *, root=None, confirm=False, reason='', request_id=None):
    """Submit the exact displayed preview; never substitute a fresh revision."""
    from uuid import uuid4
    from .control import request_digest
    if (not isinstance(preview_result, dict) or preview_result.get('node_id') != backend.node_id
            or preview_result.get('queue_id') != backend.queue_id):
        raise NodeError('WRONG_NODE', 'Control preview belongs to another node or queue')
    target = preview_result['target']
    params = {key: preview_result[key] for key in ('action', 'expected_clock_id', 'expected_revision')}
    params.update(confirm=confirm, reason=reason)
    if preview_result.get('position') is not None:
        params['position'] = preview_result['position']
    request_id = identifier(request_id or uuid4().hex, 'request ID')
    digest = request_digest(backend.node_id, backend.queue_id, target, params)
    _operation_record(root, request_id, 'control', target, params, digest)
    try:
        previous = backend.call('request', {'request_id': request_id})
        prior = previous['result']['request']
        if prior is not None:
            if prior['operation'] != 'control' or prior['digest'] != digest:
                raise NodeError('REQUEST_CONFLICT', 'Endpoint request identity names different control intent')
            return {**previous, 'result': {**prior['result'], 'replayed': True}}
        return backend.call('control', params, target, request_id=request_id)
    except NodeError as exc:
        raise NodeError(exc.code, f'{exc}; control request {request_id}. '
                        'Inspect that request on the same node and queue before issuing new intent.') from exc


def _fetch_receipt(receipt, target, spec, request_id):
    from .fetch import _validated_receipt, normalize_fetch_spec
    manifest = _validated_receipt(receipt)
    if receipt['target'] != target or receipt['fetch_id'] != request_id:
        raise NodeError('INVALID_RESPONSE', 'Fetch receipt belongs to another immutable target or request')
    selected = normalize_fetch_spec({'schema': 1, 'storage': receipt['storage'],
        'purpose': manifest['purpose'], 'capture': manifest['capture'], 'large_files': manifest['large_files'],
        'files': [{'location_id': item['source']['location_id'], 'relative': item['source']['relative'],
                   'path': item['path']} for item in manifest['files']]})
    if selected != spec:
        raise NodeError('INVALID_RESPONSE', 'Fetch receipt differs from the reviewed normalized selection')
    return receipt


def _fetch_state(path, target, spec, destination, request_id):
    _private(path)
    with path.open('rb') as handle:
        raw = handle.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise NodeError('INVALID_FETCH_STATE', 'Local fetch recovery record exceeds 512 KiB')
    value = decode(raw.rstrip(b'\n') + b'\n')
    fields = {'schema', 'request_id', 'destination', 'receipt', 'publication', 'result', 'acknowledged'}
    if (set(value) != fields or type(value['schema']) is not int or value['schema'] != 1
            or value['request_id'] != request_id or value['destination'] != str(destination)
            or type(value['acknowledged']) is not bool):
        raise NodeError('INVALID_FETCH_STATE', 'Local fetch recovery record differs from this request')
    _fetch_receipt(value['receipt'], target, spec, request_id)
    if value['acknowledged'] and (value['publication'] is None or value['result'] is None):
        raise NodeError('INVALID_FETCH_STATE', 'Acknowledged fetch lacks its local publication identity')
    return value


def _fetch_response(backend, result, target, request_id, *, pending, envelope=None, error=None):
    envelope = envelope or getattr(backend, 'last_envelope', None) or {
        'schema': 1, 'node_id': backend.node_id, 'queue_id': backend.queue_id}
    value = {**result, 'target': target, 'fetch_id': request_id, 'acknowledgment_pending': pending}
    if error is not None:
        value['acknowledgment_error'] = {'code': error.code,
            'message': f'Fetch {request_id}: replica is published; retry this same ID, specification and destination to acknowledge it. {error}'}
    return {**envelope, 'result': value}


def fetch(backend, target, spec, destination, *, root=None, request_id=None):
    """Persist capture/publication identity so acknowledgment retries never copy twice."""
    from uuid import uuid4
    from .fetch import download_fetch, download_local_fetch, normalize_fetch_spec, verify_replica
    from .protocol import target_identity
    from .transfer import _direct, _write_json
    request_id = identifier(request_id or uuid4().hex, 'fetch request ID')
    intent_recorded = False
    try:
        target = dict(target_identity(target))
        if target['node_id'] != backend.node_id or target['queue_id'] != backend.queue_id:
            raise NodeError('WRONG_NODE', 'Fetch target belongs to another node or queue')
        spec = normalize_fetch_spec(spec)
        destination = Path(destination).absolute()
        if destination != destination.resolve():
            raise NodeError('INVALID_FETCH_DESTINATION', 'Destination must be canonical without symlink traversal')
        _direct(destination.parent, directory=True)
        directory = _record_path(root, request_id)
        state_path = directory / 'fetch.json'
        existing = os.path.lexists(destination)
        if existing and not state_path.is_file():
            raise NodeError('INVALID_FETCH_DESTINATION', 'Fetch requires a new destination; existing work is never adopted')
        _operation_record(root, request_id, 'fetch', target, {'spec': spec, 'destination': str(destination)},
                          stable_hash({'target': target, 'spec': spec, 'destination': str(destination)}))
        intent_recorded = True
        state = _fetch_state(state_path, target, spec, destination, request_id) if state_path.exists() else None
        envelope = None
        if existing:
            result = verify_replica(state['receipt'], destination, state['publication'])
            state['result'] = result
            _write_json(state_path, state)
        else:
            if state is not None and state['result'] is not None:
                raise NodeError('UNVERIFIED_REPLICA', 'Previously published replica is missing; it will not be silently replaced')
            if state is None:
                envelope = backend.call('fetch.prepare', {'spec': spec}, target, request_id=request_id)
                receipt = _fetch_receipt(envelope['result'], target, spec, request_id)
                state = {'schema': 1, 'request_id': request_id, 'destination': str(destination),
                         'receipt': receipt, 'publication': None, 'result': None, 'acknowledged': False}
                _write_json(state_path, state)  # Durable capture identity precedes every download.
            else:
                # A cached capture still requires the current pinned endpoint before SFTP.
                envelope = backend.call('capabilities')
            def before_publish(publication):
                state['publication'] = publication
                _write_json(state_path, state)  # Same-volume rename will preserve this directory inode.
            if backend.connection['transport'] == 'ssh':
                result = download_fetch(backend.connection, state['receipt'], destination, _before_publish=before_publish)
            else:
                result = download_local_fetch(state['receipt'], destination, _before_publish=before_publish)
            state['result'] = result
            _write_json(state_path, state)
        if state['acknowledged']:
            return _fetch_response(backend, result, target, request_id, pending=False, envelope=envelope)
        try:
            acknowledged = backend.call('fetch.complete', {'fetch_id': request_id,
                'manifest_digest': state['receipt']['manifest_digest']}, target)
            reply = acknowledged['result']
            if (reply.get('status') != 'ACKNOWLEDGED' or reply.get('fetch_id') != request_id
                    or reply.get('manifest_digest') != state['receipt']['manifest_digest']):
                raise NodeError('INVALID_RESPONSE', 'Acknowledgment differs from the captured request')
        except (NodeError, OSError) as exc:
            error = exc if isinstance(exc, NodeError) else NodeError('FETCH_ACK_IO_ERROR', str(exc))
            return _fetch_response(backend, result, target, request_id, pending=True, envelope=envelope, error=error)
        state['acknowledged'] = True
        _write_json(state_path, state)
        return _fetch_response(backend, result, target, request_id, pending=False, envelope=acknowledged)
    except (NodeError, OSError) as exc:
        guidance = (' Retry only this same fetch ID, normalized specification and destination; '
                    'fetch captures are separate from Jobs request lookup.') if intent_recorded else ''
        raise NodeError(getattr(exc, 'code', 'FETCH_IO_ERROR'), f'Fetch {request_id}: {exc}.{guidance}') from exc
