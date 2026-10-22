"""Explicit frontend enrollment; aliases never silently resolve to localhost."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .protocol import NodeError, identifier, validate_envelope
from .transport import SSHTransport, ssh_command


def default_config():
    return Path.home() / '.config/cmw/nodes'


def _private(path, *, directory=False):
    info = path.lstat()
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise NodeError('UNSAFE_CONFIGURATION', f'Private owned configuration required: {path}')


def read_connections(root=None):
    root = Path(root) if root is not None else default_config()
    if not root.exists():
        return {}
    _private(root, directory=True)
    path = root / 'connections.json'
    if not path.exists():
        return {}
    _private(path)
    if path.stat().st_size > 128 * 1024:
        raise NodeError('INVALID_CONNECTION', 'Connection registry is too large')
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or set(record) != {'schema', 'nodes'} or record['schema'] != 1:
        raise NodeError('INVALID_CONNECTION', 'Unsupported frontend registry')
    if not isinstance(record['nodes'], dict) or len(record['nodes']) > 8:
        raise NodeError('INVALID_CONNECTION', 'At most eight explicitly configured nodes are supported')
    for alias, entry in record['nodes'].items():
        validate_connection(alias, entry)
    return record['nodes']


def validate_connection(alias, entry):
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,31}', alias):
        raise NodeError('INVALID_CONNECTION', 'Invalid display alias')
    required = {'transport', 'config_root', 'node_id', 'host_id', 'runtime_id', 'queues', 'default_queue', 'read_only'}
    if not isinstance(entry, dict) or not required <= set(entry):
        raise NodeError('INVALID_CONNECTION', 'Missing explicit endpoint enrollment fields')
    allowed = required | {'ssh_alias', 'ssh_host_key', 'executable', 'timeout', 'share_mappings'}
    if set(entry) - allowed or entry['transport'] not in {'ssh', 'local'}:
        raise NodeError('INVALID_CONNECTION', 'Unsupported connection fields or transport')
    identifier(entry['node_id'], 'node identity')
    if not isinstance(entry['host_id'], str) or not entry['host_id'] or not isinstance(entry['runtime_id'], str) or not entry['runtime_id']:
        raise NodeError('INVALID_CONNECTION', 'Pinned host and runtime identities are required')
    if type(entry['read_only']) is not bool:
        raise NodeError('INVALID_CONNECTION', 'read_only must be boolean')
    if not isinstance(entry['config_root'], str) or not entry['config_root'].startswith('/'):
        raise NodeError('INVALID_CONNECTION', 'Endpoint configuration root must be absolute')
    queues = entry['queues']
    if not isinstance(queues, dict) or not 1 <= len(queues) <= 16 or entry['default_queue'] not in queues:
        raise NodeError('INVALID_CONNECTION', 'Enrolled queue identities and a default queue are required')
    for alias, identity in queues.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', alias):
            raise NodeError('INVALID_CONNECTION', 'Invalid queue alias')
        identifier(identity, 'queue identity')
    if entry['transport'] == 'ssh':
        connection = {**entry, 'queue': entry['default_queue']}
        SSHTransport(connection)  # Validates without creating a process/connection.
    elif any(key in entry for key in ('ssh_alias', 'ssh_host_key', 'executable')):
        raise NodeError('INVALID_CONNECTION', 'Local node cannot carry SSH authority')
    if entry.get('share_mappings'):
        raise NodeError('UNSUPPORTED_MAPPING', 'Share mappings require separate endpoint/share identity qualification; use Fetch or Copy Location')
    return entry


def save_connection(root, alias, entry):
    validate_connection(alias, entry)
    root = Path(root)
    entries = read_connections(root)
    if alias in entries and entries[alias] != entry:
        raise NodeError('ENROLLMENT_CONFLICT', 'Alias is already enrolled with different endpoint evidence')
    for other_alias, other in entries.items():
        if other_alias != alias and other['node_id'] == entry['node_id']:
            raise NodeError('CLONED_NODE', 'Persistent node identity is already enrolled under another alias')
    entries[alias] = entry
    if len(entries) > 8:
        raise NodeError('INVALID_CONNECTION', 'At most eight nodes are supported')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(root, directory=True)
    path = root / 'connections.json'
    temporary = root / ('connections-' + __import__('uuid').uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'schema': 1, 'nodes': entries}, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return entry


class ConnectedBackend:
    """Narrow frontend boundary: local services and SSH share structured calls."""
    def __init__(self, alias, entry, queue=None, *, transport=None):
        validate_connection(alias, entry)
        self.alias, self.connection = alias, entry
        self.queue = queue or entry['default_queue']
        if self.queue not in entry['queues']:
            raise NodeError('UNKNOWN_QUEUE', 'Queue is not explicitly enrolled for this node')
        self.node_id, self.queue_id = entry['node_id'], entry['queues'][self.queue]
        if transport is not None:
            self.backend, self.transport = None, transport
        elif entry['transport'] == 'local':
            from .backend import LocalBackend
            self.backend, self.transport = LocalBackend(entry['config_root'], self.queue), None
        else:
            self.backend, self.transport = None, SSHTransport({**entry, 'queue': self.queue})
        self.last_envelope = None

    def call(self, operation, params=None, target=None, *, request_id=None):
        from .protocol import MUTATIONS
        if operation in MUTATIONS and self.connection['read_only']:
            raise NodeError('READ_ONLY', 'This node connection is enrolled for observation only')
        if operation in MUTATIONS:
            if request_id is None:
                from uuid import uuid4
                request_id = uuid4().hex
            verified = self.call('capabilities')
            if not verified['result'].get('mutations_supported'):
                raise NodeError('READ_ONLY', 'Endpoint does not support mutations')
            if params is not None and '_endpoint' in params:
                raise NodeError('INVALID_REQUEST', 'Endpoint guard is generated from the verified connection')
            params = {**(params or {}), '_endpoint': {
                'node_id': self.node_id, 'queue_id': self.queue_id,
                'host_id': self.connection['host_id'], 'runtime_id': self.connection['runtime_id'],
                'clock_id': verified['clock_id']}}
        if target is not None and (target.get('node_id') != self.node_id or target.get('queue_id') != self.queue_id):
            raise NodeError('WRONG_NODE', 'Selected target belongs to another node or queue')
        if self.transport:
            value = self.transport.exchange(operation, params, target, request_id=request_id)
        else:
            value = self.backend.call(operation, params, target, request_id=request_id)
        try:
            validate_envelope(value, operation)
            previous = self.last_envelope
            if previous and value['session_id'] == previous['session_id'] and value['sequence'] <= previous['sequence']:
                raise NodeError('INVALID_RESPONSE', 'Observation sequence did not advance')
        except (NodeError, ValueError, TypeError):
            self.close()
            raise
        if value.get('node_id') != self.node_id or value.get('queue_id') != self.queue_id:
            self.close()
            raise NodeError('ENDPOINT_CHANGED', 'Endpoint node or queue identity changed')
        host = value.get('host') or {}
        runtime = value.get('runtime') or {}
        if str(host.get('platform')) + ':' + str(host.get('hardware_id')) != self.connection['host_id'] or runtime.get('fingerprint') != self.connection['runtime_id']:
            self.close()
            raise NodeError('ENDPOINT_CHANGED', 'Pinned host or runtime identity changed; explicit reenrollment is required')
        self.last_envelope = value
        return value

    def close(self):
        if self.transport:
            self.transport.close()
        elif self.backend:
            self.backend.close()


def connect(alias, *, root=None, queue=None):
    entries = read_connections(root)
    if alias not in entries:
        raise NodeError('UNKNOWN_NODE', f'No explicitly enrolled node named {alias!r}')
    return ConnectedBackend(alias, entries[alias], queue)
