"""Foreground stdio endpoint; its lifetime owns observers, never execution."""
from __future__ import annotations

import sys

from cmw.jobs.store import JobsError
from .protocol import MAX_FRAME, PROTOCOL, NodeError, decode, encode, validate_request


def default_backend(config_root, queue):
    """The enrolled node kind selects its backend; a PBS node never opens a Store."""
    from .identity import load_node
    if load_node(config_root).get('kind') == 'pbs':
        from .scheduler import SchedulerBackend
        return SchedulerBackend(config_root, queue, defer_check=True)
    from .backend import LocalBackend
    return LocalBackend(config_root, queue, defer_check=True)


def serve(config_root, queue, *, input_stream=None, output_stream=None, backend_factory=None):
    if backend_factory is None:
        backend_factory = default_backend
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    backend = None
    try:
        for _ in range(4096):
            raw = incoming.readline(MAX_FRAME + 1)
            if not raw:
                return 0
            request_id = '0' * 32
            try:
                message = decode(raw)
                request_id = message.get('request_id', request_id)
                validate_request(message)
                if backend is None:
                    backend = backend_factory(config_root, queue)
                result = backend.call(message['operation'], message['params'], message['target'],
                                      request_id=request_id)
                reply = dict(protocol=PROTOCOL, request_id=request_id, ok=True, result=result)
                packet = encode(reply)
            except (NodeError, JobsError, OSError, ValueError) as exc:
                packet = encode(dict(protocol=PROTOCOL, request_id=request_id, ok=False,
                    error={'code': getattr(exc, 'code', 'NODE_REFUSED'), 'message': str(exc)[:4096]}))
            outgoing.write(packet)
            outgoing.flush()
            if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                return 2
        return 0  # Bounded session; reconnect explicitly resets observer sessions.
    finally:
        if backend is not None:
            backend.close()
