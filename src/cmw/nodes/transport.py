"""One bounded control stream per endpoint; file transfers use separate channels."""
from __future__ import annotations

import os
import re
import selectors
import shlex
import subprocess
import threading
import time

from .protocol import MAX_FRAME, MUTATIONS, NodeError, decode, encode, request, response

MAX_STDERR = 64 * 1024


def ssh_command(alias, executable, config_root, queue, *, connect_timeout=5):
    """Quote only fixed enrollment settings at OpenSSH's remote-shell boundary."""
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias):
        raise NodeError('INVALID_CONNECTION', 'Use an explicitly configured SSH alias')
    for value in (executable, config_root):
        if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 for c in value):
            raise NodeError('INVALID_CONNECTION', 'Endpoint executable/configuration must be fixed absolute paths')
    if not isinstance(queue, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', queue):
        raise NodeError('INVALID_CONNECTION', 'Invalid enrolled queue alias')
    if type(connect_timeout) is not int or not 1 <= connect_timeout <= 30:
        raise NodeError('INVALID_CONNECTION', 'Connection timeout must be between 1 and 30 seconds')
    bootstrap = shlex.join([executable, 'nodes', 'endpoint', '--config', config_root, '--queue', queue])
    return ['/usr/bin/ssh', '-T', '-v', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=1', '-o', f'ConnectTimeout={connect_timeout}', '--', alias, bootstrap]


class ProcessTransport:
    """Also used by isolated acceptance with the real endpoint on local pipes.

    This constructor is not a portable submission or arbitrary protocol callback.
    Public node connections are built from the fixed SSH bootstrap above.
    """
    def __init__(self, argv, *, timeout=10, host_key=None, env=None):
        if not 0 < timeout <= 120:
            raise NodeError('INVALID_CONNECTION', 'Operation timeout must be within 120 seconds')
        self.argv, self.timeout, self.host_key, self.env = list(argv), timeout, host_key, env
        self.process = None
        self.stderr = bytearray()
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._verified_key = host_key is None
        self._stderr_line = bytearray()
        self._verified_channel = False

    def _start(self):
        if self.process is not None:
            return
        self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                        env=self.env)
        for handle in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(handle.fileno(), False)

    def _diagnostic(self, data):
        self.stderr.extend(data)
        del self.stderr[:-MAX_STDERR]
        self._stderr_line.extend(data)
        if len(self._stderr_line) > MAX_STDERR:
            del self._stderr_line[:-MAX_STDERR]
        while b'\n' in self._stderr_line:
            line, _, rest = self._stderr_line.partition(b'\n')
            self._stderr_line[:] = rest
            if b'Server host key:' in line:
                match = re.search(rb'Server host key: \S+ (SHA256:[A-Za-z0-9+/]+)', line)
                if match and self.host_key:
                    if match.group(1).decode() != self.host_key:
                        raise NodeError('SSH_IDENTITY_CHANGED', 'SSH host key differs from enrolled transport identity')
                    self._verified_key = True

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        message = request(operation, params, target, request_id)
        packet = encode(message)
        if not self._lock.acquire(blocking=False):
            raise NodeError('BUSY', 'Only one in-flight request is permitted per endpoint channel')
        try:
            self._start()
            if operation in MUTATIONS and not self._verified_channel:
                raise NodeError('HANDSHAKE_REQUIRED', 'Verify endpoint capabilities on this channel before mutation')
            deadline, sent = time.monotonic() + self.timeout, 0
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE, 'in')
                selector.register(self.process.stdout, selectors.EVENT_READ, 'out')
                selector.register(self.process.stderr, selectors.EVENT_READ, 'err')
                while True:
                    if b'\n' in self._buffer:
                        frame, _, rest = self._buffer.partition(b'\n')
                        if rest:
                            raise NodeError('INVALID_RESPONSE', 'Unsolicited bytes follow the response')
                        self._buffer.clear()
                        value = decode(bytes(frame) + b'\n')
                        if not self._verified_key:
                            # SSH prints the key before channel output, but descriptors
                            # can become readable together; drain bounded diagnostics.
                            try:
                                self._diagnostic(os.read(self.process.stderr.fileno(), MAX_STDERR))
                            except BlockingIOError:
                                pass
                        if not self._verified_key:
                            raise NodeError('SSH_IDENTITY_UNVERIFIED', 'No matching SSH host-key evidence was observed')
                        result = response(value, message['request_id'])
                        if operation == 'capabilities':
                            self._verified_channel = True
                        return result
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NodeError('OUTCOME_UNKNOWN' if operation in MUTATIONS else 'UNREACHABLE',
                                        'Endpoint request timed out; query the original request ID before any retry')
                    for key, _ in selector.select(remaining):
                        if key.data == 'in':
                            sent += os.write(key.fd, packet[sent:])
                            if sent == len(packet):
                                selector.unregister(self.process.stdin)
                        else:
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                if key.data == 'out':
                                    raise NodeError('OUTCOME_UNKNOWN' if operation in MUTATIONS else 'DISCONNECTED',
                                                    'Endpoint closed before a complete response')
                            elif key.data == 'err':
                                self._diagnostic(data)
                            else:
                                self._buffer.extend(data)
                                if len(self._buffer) > MAX_FRAME:
                                    raise NodeError('INVALID_RESPONSE', 'Endpoint response exceeds frame bound')
        except OSError as exc:
            self.close()
            if operation in MUTATIONS:
                raise NodeError('OUTCOME_UNKNOWN', 'Transport failed; query the original request ID before retrying') from exc
            raise NodeError('UNREACHABLE', str(exc)) from exc
        except NodeError:
            self.close()
            raise
        finally:
            self._lock.release()

    def close(self):
        process, self.process = self.process, None
        self._buffer.clear()
        self._stderr_line.clear()
        self._verified_key = self.host_key is None
        self._verified_channel = False
        if process is None:
            return
        for handle in (process.stdin, process.stdout, process.stderr):
            if handle:
                handle.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()  # Only this frontend-owned SSH/endpoint process.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


class SSHTransport(ProcessTransport):
    def __init__(self, connection):
        key = connection.get('ssh_host_key')
        if not isinstance(key, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', key):
            raise NodeError('INVALID_CONNECTION', 'An explicitly enrolled SSH host-key fingerprint is required')
        super().__init__(ssh_command(connection['ssh_alias'], connection['executable'],
                                    connection['config_root'], connection['queue']),
                         timeout=connection.get('timeout', 10), host_key=key)
