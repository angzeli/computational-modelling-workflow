"""One bounded control stream per endpoint; file transfers use separate channels."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import threading
import time

from .protocol import MAX_FRAME, is_mutation, NodeError, decode, encode, request, response
from .contracts import NodeEvent
from .transport_events import TransportFailure

MAX_STDERR = 64 * 1024


SSH = '/usr/bin/ssh'
# OpenSSH expands % tokens and splits option values on whitespace, so the
# enrolled socket path is limited to characters that need neither.
_SOCKET = re.compile(r'/[A-Za-z0-9_.@+/-]{1,99}')
CHECK_TIMEOUT = 5
SESSION_CHECK = Path(__file__).with_name('session_check.sh')


def _bootstrap(alias, executable, config_root, queue, connect_timeout):
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', alias):
        raise NodeError('INVALID_CONNECTION', 'Use an explicitly configured SSH alias')
    for value in (executable, config_root):
        if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 for c in value):
            raise NodeError('INVALID_CONNECTION', 'Endpoint executable/configuration must be fixed absolute paths')
    if not isinstance(queue, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', queue):
        raise NodeError('INVALID_CONNECTION', 'Invalid enrolled queue alias')
    if type(connect_timeout) is not int or not 1 <= connect_timeout <= 30:
        raise NodeError('INVALID_CONNECTION', 'Connection timeout must be between 1 and 30 seconds')
    return shlex.join([executable, 'nodes', 'endpoint', '--config', config_root, '--queue', queue])


def ssh_command(alias, executable, config_root, queue, *, connect_timeout=5):
    """Quote only fixed enrollment settings at OpenSSH's remote-shell boundary."""
    bootstrap = _bootstrap(alias, executable, config_root, queue, connect_timeout)
    return [SSH, '-T', '-v', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=1', '-o', f'ConnectTimeout={connect_timeout}', '--', alias, bootstrap]


def control_socket(path):
    """The enrolled master socket (S02): a fixed absolute path OpenSSH reads literally."""
    if not isinstance(path, str) or not _SOCKET.fullmatch(path) or '/../' in path + '/' or '//' in path:
        raise NodeError('INVALID_CONNECTION', 'The control socket must be a plain absolute path of at most 100 '
                        'characters from letters, digits and _.@+-/')
    return path


def session_command(alias, control_path, executable, config_root, queue, *, ssh=None, connect_timeout=5):
    """Attach to the user's own OpenSSH master (S02); CMW never opens or authenticates one.

    BatchMode forbids any prompt. ProxyCommand=/usr/bin/false stops OpenSSH's
    fallback to a direct connection when the master is gone, so no network
    connection to the node is ever made outside the user's session. The master
    has already checked the host key, so StrictHostKeyChecking does not apply.
    """
    bootstrap = _bootstrap(alias, executable, config_root, queue, connect_timeout)
    return [ssh or SSH, '-T', '-v', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no',
            '-o', f'ControlPath={control_socket(control_path)}', '-o', 'ProxyCommand=/usr/bin/false',
            '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
            '-o', f'ConnectTimeout={connect_timeout}', '--', alias, bootstrap]


def open_session_command(alias, control_path):
    """The one command the user runs to open the session CMW attaches to (S03)."""
    return shlex.join(['ssh', '-M', '-S', control_socket(control_path), '-o', 'ServerAliveInterval=30',
                       '-o', 'ServerAliveCountMax=3', '-fN', alias])


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
        self._auth_exhausted = False
        self._authenticated = False

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
            # Only OpenSSH's complete pre-authentication records contribute
            # evidence. Remote endpoint stderr must not impersonate negotiation.
            if self._authenticated or self._verified_channel:
                continue
            line = line.rstrip(b'\r')
            match = re.fullmatch(rb'debug1: Server host key: \S+ (SHA256:[A-Za-z0-9+/]{43})', line)
            if match and self.host_key:
                if match.group(1).decode() != self.host_key:
                    raise NodeError('SSH_IDENTITY_CHANGED', 'SSH host key differs from enrolled transport identity')
                self._verified_key = True
            elif line == b'debug1: No more authentication methods to try.':
                self._auth_exhausted = True
            elif re.fullmatch(rb'Authenticated to .+ using "[A-Za-z0-9_-]+"\.', line):
                self._authenticated = True

    def _closed_error(self, mutation):
        # EOF and stderr may become readable together. A confirmed SSH exit,
        # pinned key and terminal negotiation stage are required for AUTH_FAILED;
        # exit 255 or a permission-denied substring alone is ambiguous.
        try:
            self._diagnostic(os.read(self.process.stderr.fileno(), MAX_STDERR))
        except (BlockingIOError, OSError):
            pass
        try:
            status = self.process.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            status = None
        if (self.host_key and status == 255 and self._verified_key
                and self._auth_exhausted and not self._authenticated):
            return TransportFailure('AUTH_FAILED', 'Pinned SSH peer refused all configured authentication methods',
                                    event=NodeEvent.AUTH_FAILURE, exit_status=status)
        return TransportFailure('OUTCOME_UNKNOWN' if mutation else 'DISCONNECTED',
            'Endpoint closed before a complete response', exit_status=status)

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        message = request(operation, params, target, request_id)
        mutation = is_mutation(operation, params)
        packet = encode(message)
        if not self._lock.acquire(blocking=False):
            raise NodeError('BUSY', 'Only one in-flight request is permitted per endpoint channel')
        try:
            self._start()
            if mutation and not self._verified_channel:
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
                        raise TransportFailure('OUTCOME_UNKNOWN' if mutation else 'UNREACHABLE',
                            'Endpoint request timed out; query the original request ID before any retry',
                            event=NodeEvent.TIMEOUT)
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
                                    raise self._closed_error(mutation)
                            elif key.data == 'err':
                                self._diagnostic(data)
                            else:
                                self._buffer.extend(data)
                                if len(self._buffer) > MAX_FRAME:
                                    raise NodeError('INVALID_RESPONSE', 'Endpoint response exceeds frame bound')
        except OSError as exc:
            # A closed SSH pipe can race the selector's stderr event. Preserve
            # an explicit key/auth refusal before reducing this to disconnection.
            try:
                if self.process is not None:
                    failure = self._closed_error(mutation)
                    if failure.code in {'AUTH_FAILED', 'AUTH_REQUIRED'}:
                        raise failure from exc
            finally:
                self.close()
            if mutation:
                raise TransportFailure('OUTCOME_UNKNOWN', 'Transport failed; query the original request ID before retrying') from exc
            raise TransportFailure('UNREACHABLE', str(exc)) from exc
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
        self._auth_exhausted = False
        self._authenticated = False
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


class SessionRequired(TransportFailure):
    """S03 AUTH_REQUIRED: no live master at the enrolled socket. Carries the one command to run."""
    def __init__(self, command):
        super().__init__('AUTH_REQUIRED', 'No open SSH session at the enrolled control socket; open it with: ' + command,
                         event=NodeEvent.SESSION_REQUIRED)
        self.command = command


class SessionTransport(ProcessTransport):
    """Connection auth mode `session` (S02/S03): every channel runs through the user's master.

    Before each connection attempt `ssh -O check` asks the master whether it is
    alive. It talks only to the local socket. If the master is missing,
    expired or not the user's own, no attempt is made. CMW cannot see the host
    key through a master (S04); the endpoint's pinned host identity is checked
    on every reply instead.
    """
    def __init__(self, connection, *, ssh=None):
        if 'ssh_host_key' in connection:
            raise NodeError('INVALID_CONNECTION', 'A session connection cannot pin a host key it never sees')
        self.alias, self.control_path = connection.get('ssh_alias'), control_socket(connection.get('control_path'))
        self.ssh = ssh or SSH
        super().__init__(session_command(self.alias, self.control_path, connection.get('executable'),
                                         connection.get('config_root'), connection.get('queue'), ssh=self.ssh),
                         timeout=connection.get('timeout', 10))
        self.open_command = open_session_command(self.alias, self.control_path)

    def master_alive(self):
        """True only if the user's master answers on a private socket; never creates one."""
        directory = os.path.dirname(self.control_path)
        try:
            folder, master = os.lstat(directory), os.lstat(self.control_path)
        except OSError:
            return False
        uid = os.geteuid()
        if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != uid or folder.st_mode & 0o077
                or not stat.S_ISSOCK(master.st_mode) or master.st_uid != uid):
            return False
        from .scheduler import run_helper  # The shell helper launches the check; Python bounds its lifetime.
        try:
            completed = run_helper(['/bin/bash', str(SESSION_CHECK), self.ssh, self.control_path, self.alias],
                                   CHECK_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _start(self):
        if self.process is None and not self.master_alive():
            raise SessionRequired(self.open_command)
        super()._start()

    def _closed_error(self, mutation):
        # The master may have gone between the check and the attempt. A read
        # that then fails is AUTH_REQUIRED only if a repeated check also fails;
        # a mutation's outcome stays unknown, because its request may have left.
        failure = super()._closed_error(mutation)
        if not mutation and failure.code == 'DISCONNECTED' and not self.master_alive():
            return SessionRequired(self.open_command)
        return failure
