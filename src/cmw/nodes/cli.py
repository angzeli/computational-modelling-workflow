"""Explicit enrollment and access to independent execution nodes."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .protocol import NodeError, decode


def _read_spec(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise NodeError('INVALID_SPEC', 'Specification must be a regular JSON file')
        raw = handle.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise NodeError('INVALID_SPEC', 'Configuration specification exceeds 128 KiB')
    value = decode(raw.rstrip(b'\n') + b'\n')
    if not isinstance(value, dict):
        raise NodeError('INVALID_SPEC', 'Specification must be an object')
    return value


def register(subcommands):
    parser = subcommands.add_parser('nodes', help='explicit enrollment and access to independent local execution nodes')
    operations = parser.add_subparsers(dest='nodes_operation', required=True)
    enroll = operations.add_parser('enroll', help='explicitly enroll this host, private queues, storage and runtime profiles')
    enroll.add_argument('--config', type=Path, required=True)
    enroll.add_argument('--spec', type=Path, required=True)
    connect = operations.add_parser('connect', help='verify and register an explicitly specified frontend connection')
    connect.add_argument('alias')
    connect.add_argument('--config', type=Path, required=True)
    connect.add_argument('--spec', type=Path, required=True)
    listing = operations.add_parser('list', help='read frontend registrations without connecting')
    listing.add_argument('--config', type=Path)
    check = operations.add_parser('check', help='read the pinned endpoint capabilities; never initializes a queue')
    check.add_argument('alias')
    check.add_argument('--config', type=Path)
    check.add_argument('--queue')
    endpoint = operations.add_parser('endpoint', help='versioned machine-only stdio endpoint; no HTTP listener')
    endpoint.add_argument('--config', type=Path, required=True)
    endpoint.add_argument('--queue', required=True)
    for command in (enroll, connect, listing, check):
        command.add_argument('--json', action='store_true')
    for command in (enroll, connect, listing, check, endpoint):
        command.set_defaults(handler=handle)


def handle(args):
    try:
        operation = args.nodes_operation
        if operation == 'endpoint':
            from .endpoint import serve
            return serve(args.config, args.queue)
        if operation == 'enroll':
            from .identity import enroll
            result = enroll(args.config, _read_spec(args.spec))
        elif operation == 'list':
            from .connections import read_connections
            result = {'schema': 1, 'nodes': read_connections(args.config)}
        elif operation == 'connect':
            from .connections import ConnectedBackend, save_connection
            specification = _read_spec(args.spec)
            backend = ConnectedBackend(args.alias, specification)
            try:
                backend.call('capabilities')
            finally:
                backend.close()
            result = save_connection(args.config, args.alias, specification)
        else:
            from .connections import connect
            backend = connect(args.alias, root=args.config, queue=args.queue)
            try:
                result = backend.call('capabilities')
            finally:
                backend.close()
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except (NodeError, OSError, ValueError) as exc:
        print(json.dumps({'code': getattr(exc, 'code', 'NODE_REFUSED'), 'message': str(exc)}))
        return 2
