"""Core-only installed catalogue commands; all metadata stays on its owner."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from .catalogue import CatalogueStore
from .catalogue_models import CatalogueQuery
from .store import JobsError


def register(operations, handler):
    query = operations.add_parser('query', help='bounded literal research catalogue search; never scans scientific files')
    query.add_argument('--text', default='', help='literal Unicode substring in names and notes')
    for field in ('node', 'queue'):
        query.add_argument('--filter-' + field, dest=field + '_id', help='exact global ' + field + ' identity filter')
    for field in ('engine', 'status', 'project', 'system', 'series', 'research-stage'):
        query.add_argument('--' + field)
    query.add_argument('--tag', action='append', default=[], help='required literal tag; repeat to require all tags')
    query.add_argument('--limit', type=int, default=50, help='page rows, 1–100')
    query.add_argument('--cursor', help='opaque cursor for this owner and query')
    query.add_argument('--sort', default='id', choices=('id', 'id_desc', 'name'))
    query.add_argument('--all-nodes', action='store_true', help='query explicitly enrolled default queues independently')
    query.add_argument('--owner-cursor', action='append', default=[], metavar='ALIAS=CURSOR',
                       help='independent owner cursor for --all-nodes; repeat per owner')
    query.add_argument('--json', action='store_true')
    query.set_defaults(handler=handler)
    catalogue = operations.add_parser('catalogue', help='explicit human organization and attempt relationships')
    actions = catalogue.add_subparsers(dest='catalogue_operation', required=True)
    for operation in ('show', 'edit', 'relations', 'relate', 'retract'):
        action = actions.add_parser(operation)
        action.add_argument('job_id')
        action.add_argument('--json', action='store_true')
        if operation in ('edit', 'relate', 'retract'):
            action.add_argument('--expected-revision', type=int, required=True)
            action.add_argument('--expected-attempt-id', required=True, help='immutable attempt identity from catalogue show')
            action.add_argument('--request-id', help='retry UUID hex; reuse only with identical parameters')
        if operation == 'edit':
            for field in ('alias', 'project', 'system', 'series', 'research-stage'):
                action.add_argument('--' + field, help='replacement human field; empty text clears it')
            action.add_argument('--tag', action='append', help='replace tags; repeat as needed')
            action.add_argument('--clear-tags', action='store_true')
        elif operation == 'relate':
            for field in ('node', 'queue', 'attempt'):
                action.add_argument('--parent-' + field, required=True, help='global parent ' + field + ' identity')
            action.add_argument('--parent-job', type=int, required=True, help='internal parent job integer')
            action.add_argument('--kind', required=True, choices=('continuation', 'repeat', 'next-stage', 'comparison'))
            action.add_argument('--evidence', type=Path, help='bounded JSON declaration of owner-local artifact records; omission is intent only')
        elif operation == 'retract':
            action.add_argument('relation_id')
        action.set_defaults(handler=handler)


def query_from_args(args):
    return CatalogueQuery(text=args.text, node_id=args.node_id, queue_id=args.queue_id,
        engine=args.engine, status=args.status, project=args.project, system=args.system,
        series=args.series, research_stage=args.research_stage, tags=tuple(args.tag),
        limit=args.limit, cursor=args.cursor, sort=args.sort)


def mutation_params(args):
    result = {'expected_revision': args.expected_revision}
    if args.catalogue_operation == 'edit':
        patch = {name: getattr(args, name) for name in
                 ('alias', 'project', 'system', 'series', 'research_stage') if getattr(args, name) is not None}
        if args.clear_tags and args.tag is not None:
            raise JobsError('--tag and --clear-tags cannot be combined')
        if args.clear_tags or args.tag is not None:
            patch['tags'] = [] if args.clear_tags else args.tag
        if not patch:
            raise JobsError('Explicit catalogue edit requires at least one replacement field')
        result['patch'] = patch
    elif args.catalogue_operation == 'relate':
        result.update(parent={'node_id': args.parent_node, 'queue_id': args.parent_queue,
                              'job_id': args.parent_job, 'attempt_id': args.parent_attempt}, kind=args.kind)
        if args.evidence is not None:
            descriptor = os.open(args.evidence, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise JobsError('Evidence declaration must be a regular file no larger than 64 KiB')
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise JobsError('Evidence declaration exceeds 64 KiB')
            result['evidence'] = json.loads(raw)
    else:
        result['relation_id'] = args.relation_id
    return result


def _print(value, args):
    from .cli import safe_text
    value = asdict(value) if hasattr(value, '__dataclass_fields__') else value
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False))
    elif 'records' in value:
        print(safe_text(f"{value['matching_count']} matching / {value['total_count']} total jobs · {value['coverage']}"))
        for row in value['records']:
            print(safe_text(f"{row['display_id']}  {row['status']}  {row['alias'] or row['name']}  {row['project'] or '—'} / {row['series'] or '—'}"))
        if value['next_cursor']:
            print('Next cursor: ' + value['next_cursor'])
    else:
        print(safe_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)))
    return 0


def local(store, args):
    service = CatalogueStore(store)
    if args.jobs_operation == 'query':
        if args.owner_cursor:
            raise JobsError('--owner-cursor requires --all-nodes')
        return _print(service.query(query_from_args(args)), args)
    action = args.catalogue_operation
    if action == 'show':
        return _print(service.read(args.job_id), args)
    from .catalogue_relations import RelationStore
    if action == 'relations':
        return _print(RelationStore(store).list(args.job_id), args)
    params = mutation_params(args)
    expected = {'expected_attempt_id': args.expected_attempt_id, 'request_id': args.request_id or uuid4().hex}
    if action == 'edit':
        result = service.edit(args.job_id, params.pop('patch'), **params, **expected)
    elif action == 'relate':
        result = RelationStore(store).add(args.job_id, **params, **expected)
    else:
        result = RelationStore(store).retract(args.job_id, params.pop('relation_id'), **params, **expected)
    return _print(result, args)


def remote(backend, args):
    from cmw.nodes.codec import decode_typed, encode_typed
    from cmw.nodes.protocol import NodeError
    if args.jobs_operation == 'query':
        if args.owner_cursor:
            raise JobsError('--owner-cursor requires --all-nodes')
        result = backend.call('catalogue.query', {'query': encode_typed(query_from_args(args))})
        return _print(decode_typed(result['result']['page']), args)
    target = backend.call('lookup', {'id': args.job_id})['result']['target']
    action = args.catalogue_operation
    params = {} if action in {'show', 'relations'} else mutation_params(args)
    expected = getattr(args, 'expected_attempt_id', None)
    if expected is not None and target['attempt_id'] != expected:
        raise NodeError('ATTEMPT_CHANGED', 'Selected immutable attempt differs from the explicitly expected target')
    operation = {'relate': 'relation.add', 'retract': 'relation.retract'}.get(action, action)
    result = backend.call('catalogue.' + operation, params, target,
                          request_id=getattr(args, 'request_id', None) or uuid4().hex)
    return _print(decode_typed(next(iter(result['result'].values()))), args)


def federation(args):
    from cmw.nodes.catalogue import federated_query
    from cmw.nodes.connections import ConnectedBackend, read_connections
    from cmw.nodes.protocol import NodeError
    if args.node or args.queue or args.state is not None:
        raise NodeError('INVALID_SELECTION', '--all-nodes selects enrolled default queues and cannot also select --node, --queue or --state')
    query = query_from_args(args)
    if query.cursor is not None:
        raise NodeError('INVALID_CURSOR', 'Federation requires separate --owner-cursor values')
    entries = read_connections(args.nodes_config)
    if not entries:
        raise NodeError('UNKNOWN_NODE', 'No nodes are explicitly enrolled; ordinary queries remain local')
    cursors = {}
    for item in args.owner_cursor:
        alias, separator, cursor = item.partition('=')
        if not separator or not cursor or alias in cursors:
            raise NodeError('INVALID_CURSOR', 'Owner cursors require unique ALIAS=CURSOR entries')
        cursors[alias] = cursor
    if set(cursors) - set(entries):
        raise NodeError('INVALID_CURSOR', 'Cursor refers to an owner outside the selected federation')
    backends = {}
    # Local endpoint construction may itself fail. Preserve that owner as unavailable.
    class Unavailable:
        def __init__(self, alias, entry, exc):
            self.alias, self.queue, self.node_id = alias, entry['default_queue'], entry['node_id']
            self.queue_id, self.error = entry['queues'][self.queue], exc
        def call(self, *args, **kwargs):
            raise self.error
        def close(self):
            pass
    try:
        for alias, entry in entries.items():
            try:
                backends[alias] = ConnectedBackend(alias, entry)
            except (NodeError, OSError, ValueError) as exc:
                backends[alias] = Unavailable(alias, entry, exc)
        result = federated_query(backends, query, cursors=cursors)
        _print(result, args)
        return 2 if result['partial'] else 0
    finally:
        for backend in backends.values():
            backend.close()
