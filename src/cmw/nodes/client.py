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
