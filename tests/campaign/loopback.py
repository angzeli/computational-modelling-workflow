#!/usr/bin/env python3
"""Task-owned unprivileged SSH fixture; no system settings or remote hosts."""
from __future__ import annotations

from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import pwd
import shlex
import signal
import socket
import subprocess
import time

import psutil


class LoopbackSSHD(AbstractContextManager):
    def __init__(self, root, *, sftp=False):
        self.root = Path(root).resolve()
        self.sftp = sftp
        self.process = None
        self.log_handle = None
        self.identities = []
        self.report = {'scope': 'Task-owned loopback SSH; no physical-node qualification',
                       'listen_address': '127.0.0.1', 'started': False, 'cleanup': []}

    def _run(self, argv, timeout=10):
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        self.report.setdefault('commands', []).append({'argv': argv, 'returncode': result.returncode,
            'stdout': result.stdout.decode(errors='replace')[-16384:],
            'stderr': result.stderr.decode(errors='replace')[-16384:]})
        return result

    def _save(self):
        path = self.root / 'report.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n')
        path.chmod(0o600)

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        for name in ('host_ed25519', 'user_ed25519'):
            path = self.root / name
            if not path.exists():
                result = self._run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)])
                if result.returncode:
                    raise RuntimeError('Task-owned key generation failed')
            path.chmod(0o600)
        authorized = self.root / 'authorized_keys'
        authorized.write_text((self.root / 'user_ed25519.pub').read_text())
        authorized.chmod(0o600)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.report['port'] = self.port
        key = (self.root / 'host_ed25519.pub').read_text().split()
        known = self.root / 'known_hosts'
        known.write_text(f'[127.0.0.1]:{self.port} {key[0]} {key[1]}\n')
        known.chmod(0o600)
        configuration = '\n'.join((
            f'Port {self.port}', 'AddressFamily inet', 'ListenAddress 127.0.0.1',
            f'HostKey {self.root / "host_ed25519"}', f'PidFile {self.root / "sshd.pid"}',
            f'AuthorizedKeysFile {authorized}', 'StrictModes yes', 'PubkeyAuthentication yes',
            'PasswordAuthentication no', 'KbdInteractiveAuthentication no', 'UsePAM no',
            'PermitRootLogin no', f'AllowUsers {self.user}', 'PermitEmptyPasswords no',
            'HostbasedAuthentication no', 'AuthenticationMethods publickey',
            'AllowAgentForwarding no', 'AllowTcpForwarding no', 'X11Forwarding no',
            'PermitTunnel no', 'PermitTTY no', 'PermitUserEnvironment no', 'PermitUserRC no',
            'UseDNS no', 'MaxAuthTries 1', 'MaxSessions 2', 'MaxStartups 2',
            'LoginGraceTime 5', 'LogLevel VERBOSE',
            'Subsystem sftp /usr/libexec/sftp-server' if self.sftp else '# SFTP not enabled for this fixture', ''))
        self.config = self.root / 'sshd_config'
        self.config.write_text(configuration)
        self.config.chmod(0o600)
        checked = self._run(['/usr/sbin/sshd', '-t', '-f', str(self.config)])
        if checked.returncode:
            self.report['blocker'] = 'Unprivileged task-owned sshd configuration check failed'
            self._save()
            raise RuntimeError(self.report['blocker'])
        self.log_path = self.root / 'sshd.stderr.log'
        self.log_handle = self.log_path.open('wb')
        self.process = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(self.config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log_handle,
            start_new_session=True)
        try:
            birth = psutil.Process(self.process.pid).create_time()
        except psutil.NoSuchProcess:
            birth = None
        self.identities.append({'pid': self.process.pid, 'birth': birth, 'role': 'task sshd parent'})
        self.report['processes'] = self.identities
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            content = self.log_path.read_text(errors='replace')[-16384:]
            if f'Server listening on 127.0.0.1 port {self.port}' in content:
                self.report['started'] = True
                self._save()
                return self
            time.sleep(.05)
        self.report['blocker'] = 'Unprivileged sshd did not establish its own loopback listener'
        self.close()
        raise RuntimeError(self.report['blocker'])

    def command(self, argv):
        """Only caller-selected trusted bootstrap argv goes through the SSH shell boundary."""
        return ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', str(self.port),
            '-o', 'BatchMode=yes', '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=3',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={self.root / "known_hosts"}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'IdentitiesOnly=yes',
            '-o', f'IdentityFile={self.root / "user_ed25519"}', '-o', 'ForwardAgent=no',
            '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
            '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
            '-l', self.user, '127.0.0.1', shlex.join(argv)]

    def close(self):
        if self.process is not None and self.process.poll() is None:
            identity = self.identities[0]
            try:
                parent = psutil.Process(identity['pid'])
                if parent.create_time() != identity['birth']:
                    self.report['cleanup'].append({'pid': identity['pid'], 'status': 'identity changed; not signalled'})
                else:
                    children = parent.children(recursive=True)
                    for child in children:
                        self.identities.append({'pid': child.pid, 'birth': child.create_time(), 'role': 'task sshd descendant'})
                    for item in reversed(self.identities):
                        try:
                            process = psutil.Process(item['pid'])
                            if process.create_time() != item['birth']:
                                self.report['cleanup'].append({'pid': item['pid'], 'status': 'identity changed; not signalled'})
                                continue
                            process.send_signal(signal.SIGTERM)
                            self.report['cleanup'].append({'pid': item['pid'], 'status': 'SIGTERM to verified task-owned identity'})
                        except psutil.NoSuchProcess:
                            self.report['cleanup'].append({'pid': item['pid'], 'status': 'already exited'})
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.report['cleanup'].append({'pid': identity['pid'], 'status': 'termination not confirmed; inspect retained evidence'})
            except psutil.NoSuchProcess:
                self.report['cleanup'].append({'pid': identity['pid'], 'status': 'already exited'})
        if self.process is not None:
            self.report['server_returncode'] = self.process.poll()
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
        if hasattr(self, 'log_path'):
            self.report['server_log_tail'] = self.log_path.read_text(errors='replace')[-16384:]
        self.report['remaining_verified_processes'] = []
        for item in self.identities:
            try:
                process = psutil.Process(item['pid'])
                if process.create_time() == item['birth'] and process.status() != psutil.STATUS_ZOMBIE:
                    self.report['remaining_verified_processes'].append(item)
            except psutil.NoSuchProcess:
                pass
        self._save()

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.report['exception'] = f'{exc_type.__name__}: {exc}'
        self.close()
        return False


def main():
    fixture = LoopbackSSHD(Path(__file__).parent)
    try:
        with fixture:
            result = fixture._run(fixture.command(['/usr/bin/true']))
            fixture.report['true_handshake_passed'] = result.returncode == 0
            if result.returncode:
                fixture.report['blocker'] = 'Loopback listener started, but unprivileged public-key SSH login failed'
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        fixture.report.setdefault('blocker', f'{type(exc).__name__}: {exc}')
        fixture.close()
    print(json.dumps({key: fixture.report.get(key) for key in (
        'started', 'true_handshake_passed', 'blocker', 'server_returncode', 'remaining_verified_processes')}, indent=2))
    return 0 if fixture.report.get('true_handshake_passed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
