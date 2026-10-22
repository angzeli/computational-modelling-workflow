"""Explicit node-selected Jobs commands; ordinary local routing stays unchanged."""
from __future__ import annotations

import json

from .connections import connect
from .protocol import NodeError


def watch(args):
    from .connections import ConnectedBackend, read_connections
    try:
        from .portal import NodesApp
    except ModuleNotFoundError as exc:
        raise NodeError('CONSOLE_UNAVAILABLE', 'Console dependency missing; install the CMW jobs extra') from exc
    entries = read_connections(args.nodes_config)
    if args.all_nodes:
        if args.node or args.queue:
            raise NodeError('INVALID_SELECTION', '--all-nodes cannot also select --node or --queue')
        if not entries:
            raise NodeError('UNKNOWN_NODE', 'No nodes are explicitly enrolled; ordinary jobs watch remains local')
        selected = entries
    else:
        if not args.node or args.node not in entries:
            raise NodeError('UNKNOWN_NODE', 'Select an explicitly enrolled node')
        selected = {args.node: entries[args.node]}
    backends = {}
    try:
        for alias, entry in selected.items():
            backends[alias] = ConnectedBackend(alias, entry, args.queue if not args.all_nodes else None)
        NodesApp(backends, request_root=args.nodes_config).run()
    finally:
        for backend in backends.values():
            backend.close()
    return 0


def handle(args):
    backend = None
    try:
        if args.state is not None:
            raise NodeError('INVALID_SELECTION', '--state is local only; select an enrolled remote --queue')
        if args.jobs_operation == 'watch':
            return watch(args)
        if not args.node:
            raise NodeError('INVALID_SELECTION', '--queue and --all-nodes require explicit node selection')
        backend = connect(args.node, root=args.nodes_config, queue=args.queue)
        operation = args.jobs_operation
        if operation == 'submit':
            from .client import submit
            from .cli import _read_spec
            envelope = submit(backend, _read_spec(args.spec), root=args.nodes_config, preview=args.preview)
        elif operation == 'request':
            envelope = backend.call('request', {'request_id': args.request_id})
        elif operation == 'status':
            envelope = backend.call('status')
        elif operation == 'fetch':
            from .client import fetch
            from .cli import _read_spec
            target = backend.call('lookup', {'id': args.job_id})['result']['target']
            envelope = fetch(backend, target, _read_spec(args.spec), args.destination,
                             root=args.nodes_config, request_id=args.request_id)
        elif operation in {'hold', 'release', 'move', 'cancel', 'suspend', 'continue', 'start', 'stop', 'pause', 'resume'}:
            from .client import apply_control
            queue_actions = {'start': 'controller.start', 'stop': 'controller.stop',
                             'pause': 'dispatch.pause', 'resume': 'dispatch.resume'}
            target = None
            if operation not in queue_actions:
                selected = backend.call('lookup', {'id': args.job_id})['result']
                target = selected['target']
                if getattr(args, 'expected_attempt_id', None) not in (None, target['attempt_id']):
                    raise NodeError('ATTEMPT_CHANGED', 'Expected attempt differs from the selected immutable target')
                expected_suspension = getattr(args, 'expected_revision', None)
                if expected_suspension is not None and expected_suspension != (selected['job'].get('suspension') or {}).get('revision'):
                    raise NodeError('REVISION_CONFLICT', 'Suspension revision differs from the explicitly expected value')
            params = {'action': queue_actions.get(operation, operation)}
            if operation == 'move':
                params['position'] = args.position
            envelope = backend.call('control.preview', params, target)
            if not args.preview:
                envelope = apply_control(backend, envelope['result'], root=args.nodes_config,
                    confirm=getattr(args, 'yes', False), reason=getattr(args, 'reason', ''), request_id=args.request_id)
        elif operation in {'show', 'observe', 'logs', 'files', 'progress'}:
            found = backend.call('lookup', {'id': args.job_id})
            target = found['result']['target']
            params = {}
            wire_operation = operation
            if operation == 'files':
                if args.open:
                    raise NodeError('REMOTE_LOCATION', 'Remote paths cannot be opened as local folders; use an explicitly verified share or Fetch')
                wire_operation = 'locations'
            if operation == 'logs':
                params = {'stream': args.stream, 'bytes': args.bytes}
            if operation == 'progress':
                if args.source or args.engine:
                    raise NodeError('SOURCE_SELECTION', 'Remote Sources must be validated by the owning endpoint through its source-selection operation')
                if args.history:
                    wire_operation = 'history'
                    params = {'cursor': args.cursor, 'limit': args.limit, 'parent_id': args.parent,
                              'stage': args.stage, 'segment': args.segment}
                    params = {key: value for key, value in params.items() if value is not None}
            envelope = backend.call(wire_operation, params, target)
        else:
            raise NodeError('UNSUPPORTED_OPERATION', f'Node-selected operation is not yet available: {operation}')
        if args.json:
            print(json.dumps(envelope, indent=2, ensure_ascii=True, allow_nan=False))
        elif operation == 'status':
            from cmw.jobs.cli import status_text
            print(f"NODE {backend.alias} / {backend.queue} ({backend.node_id})")
            print(status_text(envelope['result']['state']))
        elif operation == 'progress':
            from cmw.progress.formatting import progress_text, history_text
            from .codec import decode_typed
            if args.history:
                print(history_text(decode_typed(envelope['result']['page'])))
            else:
                print(progress_text(decode_typed(envelope['result']['snapshot']), now=envelope['observed_at']))
        elif operation == 'logs':
            print(envelope['result']['text'])
        else:
            print(json.dumps(envelope, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except (NodeError, OSError, ValueError) as exc:
        print(json.dumps({'code': getattr(exc, 'code', 'NODE_REFUSED'), 'message': str(exc)}))
        return 2
    finally:
        if backend is not None:
            backend.close()
