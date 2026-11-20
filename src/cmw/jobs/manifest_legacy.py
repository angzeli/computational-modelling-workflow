"""Legacy queue bindings and queue-qualified legacy IDs (#41 Phase 4a, #76).

Independent queue databases reuse the same local integers, so a legacy job is
named by its origin, ``<node>/<queue>:J<n>.<m>``. The project owner records
each of its local queues once as a ``queue_binding`` receipt in the project
integrity journal: the state root, the root and database ``[device, inode]``
pairs and the containing volume UUID. Device numbers are diagnostic only;
identity is volume UUID plus inode, which survives an external-volume remount.
The resolver reads a bound queue database without writing and returns its one
matching record. Short ``<node>:J<n>.<m>`` forms, relocation (``queue_locator``),
other nodes' queues and the project file's legacy maps are deferred.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from .manifests import KEY, ManifestError, _require_owner, append_journal, load_project, read_journal, request_uuid
from .store import JobsError, Store

QUALIFIED = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*):J([1-9]\d*)\.([1-9]\d*)')
VOLUME_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
# Shown by `manifest resolve`; argv/env stay out of the output because they may be sensitive.
SUMMARY_FIELDS = ('id', 'display_id', 'attempt_id', 'name', 'engine', 'status', 'cwd', 'started_at',
                  'finished_at', 'exit_code')


def order_key(job_id):
    """Numeric project order: integer component, then legacy suffix, node and queue tokens."""
    match = QUALIFIED.fullmatch(job_id)
    if match:
        return int(match[3]), int(match[4]), match[1], match[2]
    return int(job_id[1:]), 0, '', ''


def observe_volume_uuid(path):
    """The normalized volume UUID holding path, observed by the shell helper; None when unavailable."""
    script = Path(__file__).with_name('manifest_volume.sh')
    try:
        completed = subprocess.run(['/bin/bash', str(script), str(path)], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=30, cwd='/')
        value = json.loads(completed.stdout or '{}').get('volume_uuid') if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    value = value.lower() if isinstance(value, str) else None
    return value if value and VOLUME_UUID.fullmatch(value) else None


def _token(value, name):
    if not isinstance(value, str) or len(value) > 128 or not KEY.fullmatch(value):
        raise ManifestError(f'{name} token must match [A-Za-z0-9][A-Za-z0-9._-]* (at most 128)')
    return value


def _observe(state_root, volume_uuid_of):
    """Current root/database identities and volume UUID of a queue state directory."""
    root = Path(state_root).expanduser()
    try:
        resolved = root.resolve(strict=True)
        root_info, database_info = resolved.lstat(), (resolved / 'queue.sqlite3').lstat()
    except OSError as exc:
        raise ManifestError(f'Queue state root is unavailable: {exc}') from exc
    if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISREG(database_info.st_mode):
        raise ManifestError(f'{resolved} is not a queue state directory with a regular queue.sqlite3')
    if root_info.st_dev != database_info.st_dev:
        raise ManifestError(f'{resolved}/queue.sqlite3 is on another filesystem than its state root')
    volume = (volume_uuid_of or observe_volume_uuid)(resolved)
    if not isinstance(volume, str) or not VOLUME_UUID.fullmatch(volume):
        raise ManifestError(f'The volume UUID holding {resolved} is unknown; a binding needs it and none is invented')
    return resolved, {'root_identity': [root_info.st_dev, root_info.st_ino],
                      'database_identity': [database_info.st_dev, database_info.st_ino], 'volume_uuid': volume}


def _enrollment(resolved):
    """The validated enrollment record, None for an unenrolled queue; a moved or copied enrolled queue refuses."""
    try:
        return Store(resolved).queue_identity()
    except JobsError as exc:
        raise ManifestError(f'Queue at {resolved}: {exc}') from exc


def _recorded_queue_id(con):
    """The enrolled queue UUID stored in the queue itself, read without the Store's device-number check.

    Store.queue_identity compares full [device, inode] pairs, which a remount may change;
    resolution compares volume UUID plus inode instead.
    """
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
        return None
    row = con.execute("SELECT value FROM meta WHERE key='queue_identity'").fetchone()
    record = json.loads(row[0]) if row is not None else None
    return record.get('queue_id') if isinstance(record, dict) else None


def _same(binding, observed):
    return (binding['volume_uuid'] == observed['volume_uuid']
            and binding['root_identity'][1] == observed['root_identity'][1]
            and binding['database_identity'][1] == observed['database_identity'][1])


def bindings(records):
    """(node, queue) -> initial binding plus its journal sequence, from complete journal records."""
    found = {}
    for record in records:
        if record.get('event') == 'queue_binding':
            payload = record['payload']
            found[(payload['node'], payload['queue'])] = {**payload['binding'], 'sequence': record['sequence']}
    return found


def bind_queue(store, root, *, node, queue, state_root, request=None, volume_uuid_of=None):
    """Record one of the owner's local legacy queues once; rebinding or aliasing is refused."""
    root, project = load_project(root)
    _require_owner(store, project)
    node, queue = _token(node, 'Node'), _token(queue, 'Queue')
    request = request_uuid(request)
    resolved, observed = _observe(state_root, volume_uuid_of)
    enrollment = _enrollment(resolved)
    if enrollment is not None and enrollment['node_id'] != project['owner_node_id']:
        raise ManifestError(f'Queue at {resolved} is enrolled to node {enrollment["node_id"]}, not the project '
                            "owner; only the owner's local queues can be bound")
    queue_id = enrollment['queue_id'] if enrollment else None
    try:  # A binding must name a readable Jobs queue, observed without writing.
        with Store(resolved).read_transaction() as con:
            con.execute('SELECT count(*) FROM jobs').fetchone()
    except (JobsError, OSError, sqlite3.Error) as exc:
        raise ManifestError(f'Queue at {resolved} is not readable as a Jobs queue: {exc}') from exc
    binding = {'queue_id': queue_id, 'state_root': str(resolved), **observed}
    payload = {'node': node, 'queue': queue, 'binding': binding}

    def admit(records):
        for (bound_node, bound_queue), existing in bindings(records).items():
            if (bound_node, bound_queue) == (node, queue):
                raise ManifestError(f'{node}/{queue} is already bound (journal record {existing["sequence"]}); '
                                    'rebinding is refused and relocation (queue_locator) is not available yet')
            if (existing['volume_uuid'], existing['database_identity'][1]) == (
                    observed['volume_uuid'], observed['database_identity'][1]):
                raise ManifestError(f'This queue database is already bound as {bound_node}/{bound_queue}; '
                                    'one database is one queue')
            if bound_node != node:
                raise ManifestError(f"The owner's queues are bound under node token {bound_node!r}; only the "
                                    "owner's local queues can be bound, under one token")

    record = append_journal(root, project['project_key'], 'queue_binding', request, payload, admit)
    return {'project': project['project_key'], 'queue': f'{node}/{queue}', 'sequence': record['sequence'],
            'request_uuid': request, 'binding': record['payload']['binding']}


def resolve(root, reference, *, volume_uuid_of=None):
    """The one queue record a ``<node>/<queue>:J<n>.<m>`` reference names, read without writing."""
    root, project = load_project(root)
    match = QUALIFIED.fullmatch(reference) if isinstance(reference, str) else None
    if match is None:
        raise ManifestError('Legacy references are queue-qualified <node>/<queue>:J<n>.<m>; '
                            'bare and short node-qualified forms are not resolved')
    node, queue, number, display = match[1], match[2], int(match[3]), reference.split(':', 1)[1]
    binding = bindings(read_journal(root)).get((node, queue))
    if binding is None:
        raise ManifestError(f'Queue {node}/{queue} has no binding in this project (cmw jobs manifest bind-queue)')
    state_root = Path(binding['state_root'])

    def differs(reason):
        return ManifestError(f'Queue {node}/{queue} at {state_root} no longer matches its binding ({reason}); '
                             'relocation needs a reviewed queue_locator, which is not available yet')

    def verify():  # Before and after the read, so a database swapped in between is refused.
        try:
            resolved, observed = _observe(state_root, volume_uuid_of)
        except ManifestError as exc:
            raise differs(exc) from exc
        if resolved != state_root or not _same(binding, observed):
            raise differs('volume UUID or inode differs')

    verify()
    try:
        with Store(state_root).read_transaction() as con:
            queue_id = _recorded_queue_id(con)
            rows = [json.loads(row[0]) for row in con.execute(
                "SELECT data FROM jobs WHERE id=? OR json_extract(data, '$.display_id')=?", (number, display))]
    except (JobsError, sqlite3.Error, ValueError) as exc:
        raise ManifestError(f'Queue {node}/{queue} is not readable: {exc}') from exc
    verify()
    if binding['queue_id'] is not None and queue_id != binding['queue_id']:
        raise differs('enrolled queue UUID differs')
    if not rows:
        raise ManifestError(f'Queue {node}/{queue} has no job {display}')
    if len(rows) > 1:
        raise ManifestError(f'Queue {node}/{queue} has {len(rows)} records for {display}; '
                            'duplicate display IDs are refused')
    job = rows[0]
    if not isinstance(job, dict) or job.get('id') != number or job.get('display_id') != display:
        shown = job.get('display_id') if isinstance(job, dict) else None
        raise ManifestError(f'Queue {node}/{queue} job {number} is displayed as {shown!r}, not {display}; '
                            'renamed display IDs need explicit mapping review')
    return {'reference': reference, 'node': node, 'queue': queue, 'binding_sequence': binding['sequence'],
            'job': job}


def summary(resolved):
    """The resolver result without argv/env, for display."""
    return {**{key: value for key, value in resolved.items() if key != 'job'},
            'job': {key: resolved['job'].get(key) for key in SUMMARY_FIELDS}}
