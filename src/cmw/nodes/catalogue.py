"""Owner-local catalogue operations and bounded, explicitly non-atomic federation."""
from __future__ import annotations

from dataclasses import asdict, replace
from threading import Event

from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery, CatalogueRecord, CataloguePage
from .codec import decode_typed, encode_typed
from .protocol import NodeError

CAPABILITY = {'schema': 1, 'query': 'CatalogueQuery', 'page': 'CataloguePage', 'record': 'CatalogueRecord'}


def dispatch(backend, operation, params, target, *, request_id=None):
    from .backend import _params
    service = CatalogueStore(backend.store)
    if operation == 'catalogue.query':
        _params(params, ('query',), ('query',))
        if target is not None:
            raise NodeError('INVALID_TARGET', 'Catalogue query does not accept an attempt target')
        query = decode_typed(params['query'], CatalogueQuery)
        return {'page': encode_typed(service.query(query))}
    job = backend._job(target)
    expected = {'expected_attempt_id': job['attempt_id']}
    if operation == 'catalogue.show':
        _params(params)
        result = {'record': encode_typed(service.read(job['id'], **expected))}
    elif operation == 'catalogue.edit':
        _params(params, ('patch', 'expected_revision'), ('patch', 'expected_revision'))
        record = service.edit(job['id'], params['patch'], expected_revision=params['expected_revision'],
                              request_id=request_id, **expected)
        result = {'record': encode_typed(record)}
    else:
        from cmw.jobs.catalogue_relations import RelationStore
        relations = RelationStore(backend.store)
        if operation == 'catalogue.relations':
            _params(params)
            page = relations.list(job['id'], **expected)
        elif operation == 'catalogue.relation.add':
            _params(params, ('parent', 'kind', 'expected_revision', 'evidence'), ('parent', 'kind', 'expected_revision'))
            page = relations.add(job['id'], parent=params['parent'], kind=params['kind'],
                expected_revision=params['expected_revision'], request_id=request_id,
                evidence=params.get('evidence'), **expected)
        elif operation == 'catalogue.relation.retract':
            _params(params, ('relation_id', 'expected_revision'), ('relation_id', 'expected_revision'))
            page = relations.retract(job['id'], params['relation_id'],
                expected_revision=params['expected_revision'], request_id=request_id, **expected)
        else:
            raise NodeError('UNSUPPORTED_OPERATION', 'Unknown research catalogue operation')
        result = {'relations': encode_typed(page)}
    backend._job(target)
    return result


def _model(operation):
    if operation == 'catalogue.query':
        return 'page', CataloguePage
    if operation in {'catalogue.show', 'catalogue.edit'}:
        return 'record', CatalogueRecord
    from cmw.jobs.catalogue_relations import RelationPage
    return 'relations', RelationPage


def validate_result(envelope, operation):
    key, model = _model(operation)
    try:
        if set(envelope['result']) != {key}:
            raise ValueError('Unexpected catalogue response fields')
        record = decode_typed(envelope['result'][key], model)
        if (record.node_id, record.queue_id) != (envelope['node_id'], envelope['queue_id']):
            raise ValueError('Catalogue observation belongs to another owner')
        if isinstance(record, CataloguePage):
            for row in record.records:
                if (row.node_id, row.queue_id) != (record.node_id, record.queue_id):
                    raise ValueError('Catalogue page contains a foreign target')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NodeError('INVALID_RESPONSE', 'Malformed or misbound research catalogue response') from exc


def validate_selected_result(envelope, operation, target):
    key, model = _model(operation)
    record = decode_typed(envelope['result'][key], model)
    if any(getattr(record, key, None) != value for key, value in target.items()):
        raise NodeError('INVALID_RESPONSE', 'Catalogue response differs from the selected immutable attempt')


def federated_query(backends, query, *, cursors=None, cancel: Event | None = None):
    """Query at most eight enrolled owners; preserve independent cursors and failures.

    Callers own connection lifetime. Cancellation stops subsequent owner requests;
    an already in-flight transport retains its normal bounded timeout. No attempt
    is inferred missing merely because an owner cannot be reached.
    """
    if type(query) is not CatalogueQuery or not isinstance(backends, dict) or not 1 <= len(backends) <= 8:
        raise NodeError('INVALID_SELECTION', 'Federation requires a query and one to eight explicit owner queues')
    cursors = {} if cursors is None else cursors
    if type(cursors) is not dict or set(cursors) - set(backends) or any(
            value is not None and (type(value) is not str or len(value) > 16384) for value in cursors.values()):
        raise NodeError('INVALID_CURSOR', 'Federation cursors must name only selected owners')
    if query.cursor is not None:
        raise NodeError('INVALID_CURSOR', 'Federation uses per-owner cursors, never one global cursor')
    results = []
    for alias, backend in backends.items():
        origin = {'alias': alias, 'node_id': backend.node_id, 'queue_id': backend.queue_id,
                  'queue': backend.queue}
        if cancel is not None and cancel.is_set():
            results.append({**origin, 'coverage': 'CANCELLED', 'page': None,
                            'error': {'code': 'CANCELLED', 'message': 'Superseded search; owner was not queried'}})
            continue
        try:
            page = decode_typed(backend.call('catalogue.query', {
                'query': encode_typed(replace(query, cursor=cursors.get(alias)))})['result']['page'], CataloguePage)
            results.append({**origin, 'coverage': page.coverage, 'page': asdict(page), 'error': None})
        except (NodeError, OSError, ValueError) as exc:
            results.append({**origin, 'coverage': 'UNAVAILABLE', 'page': None,
                            'error': {'code': getattr(exc, 'code', 'CATALOGUE_UNAVAILABLE'), 'message': str(exc)}})
    return {'schema': 1, 'consistency': 'independent owner pages; not an atomic multi-node snapshot',
            'partial': any(row['page'] is None for row in results), 'owners': results}
