"""Explicit node-selected Jobs commands; ordinary local routing stays unchanged."""
from __future__ import annotations

import json

from .connections import connect
from .protocol import NodeError


def handle(args):
    backend = None
    try:
        if args.state is not None:
            raise NodeError('INVALID_SELECTION', '--state is local only; select an enrolled remote --queue')
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
