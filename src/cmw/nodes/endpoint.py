"""Foreground stdio endpoint; its lifetime owns observers, never execution."""
from __future__ import annotations

import io
import os
import select
import sys
import time

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


def idle_exit_seconds(config_root):
    """How long a scheduler-backed endpoint waits for a request before it exits (#81).

    Nothing CMW-owned may stay on a shared login node after its client is gone,
    even when the network drops without closing the SSH channel. Other node
    kinds keep waiting until the frontend closes the stream. An endpoint whose
    configuration cannot be read serves nothing, so it is bounded as well.
    """
    from .identity import load_node
    from .scheduler import idle_exit_seconds as scheduler_idle, DEFAULT_IDLE_EXIT_SECONDS
    try:
        config = load_node(config_root)
    except (NodeError, OSError, ValueError, KeyError, TypeError):
        return DEFAULT_IDLE_EXIT_SECONDS
    return scheduler_idle(config) if config.get('kind') == 'pbs' else None


class _IdleReader:
    """`readline(limit)` that returns None when no complete line arrives in time."""

    def __init__(self, fd, seconds, clock=time.monotonic):
        self.fd, self.seconds, self.clock = fd, seconds, clock
        self.buffer, self.eof = bytearray(), False

    def readline(self, limit):
        deadline = self.clock() + self.seconds
        while True:
            newline = self.buffer.find(b'\n', 0, limit)
            if newline >= 0:
                cut = newline + 1
            elif len(self.buffer) >= limit or self.eof:
                cut = limit  # As readline: an over-long or final partial line is returned as it is.
            else:
                cut = None
            if cut is not None:
                line = bytes(self.buffer[:cut])
                del self.buffer[:cut]
                return line
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            if select.select([self.fd], [], [], remaining)[0]:
                chunk = os.read(self.fd, 65536)
                if chunk:
                    self.buffer.extend(chunk)
                else:
                    self.eof = True


def _reader(incoming, idle_seconds):
    if idle_seconds is None:
        return incoming.readline
    try:
        fd = incoming.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return incoming.readline  # In-memory test streams never stall.
    return _IdleReader(fd, idle_seconds).readline


def serve(config_root, queue, *, input_stream=None, output_stream=None, backend_factory=None, idle_seconds=None):
    if backend_factory is None:
        backend_factory = default_backend
    if idle_seconds is None:
        idle_seconds = idle_exit_seconds(config_root)
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    readline = _reader(incoming, idle_seconds)
    backend = None
    try:
        for _ in range(4096):
            raw = readline(MAX_FRAME + 1)
            if not raw:
                return 0  # EOF, or idle past the bound: the observer session ends; nothing persists.
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
