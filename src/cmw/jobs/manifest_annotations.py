"""Owner-written annotations.json for native manifests (#41 Phase 3a, #69).

Human judgement (label, purpose, system, outcome, note) as an append-only
event history beside the job's anchored job.json on the project owner. It is
separate from the replaceable queue note: nothing here writes or migrates that
note, and nothing here controls execution. Each request UUID is committed as a
receipt in the project ledger before the file is published, so a lost reply
is retried with the same UUID and a crash between the two is reconciled by the
next append. Appends are serialized by a per-job lock and published atomically
inside ``.cmw-manifest/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

from .catalogue_models import text_value
from .manifest_state import _locked, _write
from .manifests import (MANIFEST_DIR, NATIVE_ID, SCHEMA_VERSION, ManifestError, _ledger, _meta,
                        _require_owner, _utc_now, canonical, load_project, read_journal, request_uuid)
from .store import validate_note

FIELDS = ('label', 'purpose', 'system', 'outcome', 'note')
OUTCOMES = ('reported', 'superseded', 'checkpoint-only', 'discarded')
DOCUMENT_KEYS = {'schema_version', 'id', 'project', 'provenance', 'revision', 'events'}
EVENT_KEYS = {'sequence', 'timestamp', 'author_node', *FIELDS}
# Ledgers created before Phase 3a gain this table on their first annotation.
RECEIPTS = ('CREATE TABLE IF NOT EXISTS annotation_requests(request_uuid TEXT PRIMARY KEY, job_id TEXT NOT NULL, '
            'sequence INTEGER NOT NULL, intent_sha256 TEXT NOT NULL, event TEXT NOT NULL, UNIQUE(job_id, sequence))')


def _changes(changes):
    """Validated event fields; null means no change, an empty string clears (outcome excepted)."""
    if not isinstance(changes, dict) or set(changes) - set(FIELDS):
        raise ManifestError(f'Annotation changes may only name {", ".join(FIELDS)}')
    fields = {}
    for key in FIELDS:
        value = changes.get(key)
        if value is not None:
            if key == 'outcome':
                if value not in OUTCOMES:
                    raise ManifestError(f'Outcome must be one of {", ".join(OUTCOMES)}; '
                                        'clearing an outcome is not supported')
            elif key == 'note':
                value = validate_note(value)
            else:
                try:
                    value = text_value(value, key.capitalize())
                except ValueError as exc:
                    raise ManifestError(str(exc)) from exc
        fields[key] = value
    if all(value is None for value in fields.values()):
        raise ManifestError(f'An annotation needs at least one of {", ".join(FIELDS)}')
    return fields


def current(events):
    """The displayed values: events folded in sequence, cleared fields shown as null."""
    view = dict.fromkeys(FIELDS)
    for event in events:
        for key in FIELDS:
            if event[key] is not None:
                view[key] = event[key] or None
    return view


def _sidecar(root, job_id):
    """The owner's sidecar for an anchored native job, with job.json matching its receipt."""
    if not isinstance(job_id, str) or not NATIVE_ID.fullmatch(job_id):
        raise ManifestError('Manifest annotations take a native project ID (J<n>)')
    sidecar = root / 'jobs' / job_id / MANIFEST_DIR
    for path in (sidecar.parent.parent, sidecar.parent, sidecar):
        try:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ManifestError(f'{path} must be a real directory')
        except FileNotFoundError:
            raise ManifestError(f'{job_id} has no manifest on this project owner') from None
    anchors = [record for record in read_journal(root)
               if record['event'] == 'manifest' and record['payload']['id'] == job_id]
    if not anchors:
        raise ManifestError(f'{job_id} has no manifest receipt; nothing to annotate')
    try:
        raw = (sidecar / 'job.json').read_bytes()
    except OSError as exc:
        raise ManifestError(f'{job_id} job.json is unreadable on the owner: {exc}') from exc
    if hashlib.sha256(raw).hexdigest() != anchors[0]['payload']['job_sha256']:
        raise ManifestError(f'{job_id} job.json differs from its integrity receipt; annotation refused')
    return sidecar


def _load(path, project_key, job_id, provenance='native'):
    if not os.path.lexists(path):
        return {'schema_version': SCHEMA_VERSION, 'id': job_id, 'project': project_key,
                'provenance': provenance, 'revision': 0, 'events': []}
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('not a regular file')
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ManifestError(f'{path} is unreadable ({exc}); explicit review is required') from exc
    events = document.get('events') if isinstance(document, dict) else None
    if (not isinstance(document, dict) or set(document) != DOCUMENT_KEYS
            or document['schema_version'] != SCHEMA_VERSION
            or (document['id'], document['project'], document['provenance']) != (job_id, project_key, provenance)
            or not isinstance(events, list) or document['revision'] != len(events)
            or any(not isinstance(event, dict) or set(event) != EVENT_KEYS or event['sequence'] != index
                   for index, event in enumerate(events, 1))):
        raise ManifestError(f'{path} is not a consistent append-only history; explicit review is required')
    return document


def annotate(store, root, job_id, changes, *, expected_revision=None, request=None):
    """Append one human event; identical retries of a request UUID return its original event."""
    root, project = load_project(root)
    node = _require_owner(store, project)['node_id']
    fields = _changes(changes)
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ManifestError('Expected annotation revision must be a nonnegative integer')
    request = request_uuid(request)
    intent = hashlib.sha256(canonical({'id': job_id, 'expected_revision': expected_revision, **fields})).hexdigest()
    sidecar = _sidecar(root, job_id)
    path = sidecar / 'annotations.json'
    with _locked(sidecar, 'annotations.lock'):
        document = _load(path, project['project_key'], job_id)
        event = None
        with _ledger(root) as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                meta = _meta(con)
                if (meta.get('project_key'), meta.get('owner_node_id')) != (project['project_key'],
                                                                            project['owner_node_id']):
                    raise ManifestError('Project ID ledger belongs to a different project or owner')
                con.execute(RECEIPTS)
                receipts = [json.loads(row[0]) for row in con.execute(
                    'SELECT event FROM annotation_requests WHERE job_id=? ORDER BY sequence', (job_id,))]
                # Every published event must be exactly the one its receipt recorded.
                if receipts[:document['revision']] != document['events']:
                    raise ManifestError(f'{path} differs from its ledger receipts; explicit review is required')
                # Receipts committed by an append whose file publication did not finish.
                unpublished = receipts[document['revision']:]
                replay = con.execute('SELECT intent_sha256, sequence FROM annotation_requests WHERE request_uuid=?',
                                     (request,)).fetchone()
                if replay is not None and replay[0] != intent:
                    raise ManifestError('Request UUID already recorded a different annotation')
                if replay is None:
                    revision = document['revision'] + len(unpublished)
                    if expected_revision is not None and expected_revision != revision:
                        raise ManifestError(f'{job_id} annotations are at revision {revision}, not {expected_revision}; '
                                            'reload them before appending')
                    shown = current(document['events'] + unpublished)
                    if all(value is None or (value or None) == shown[key] for key, value in fields.items()):
                        raise ManifestError('This annotation changes nothing; no event was appended')
                    event = {'sequence': revision + 1, 'timestamp': _utc_now(), 'author_node': node, **fields}
                    con.execute('INSERT INTO annotation_requests VALUES (?, ?, ?, ?, ?)',
                                (request, job_id, event['sequence'], intent, canonical(event).decode()))
                con.execute('COMMIT')
            except BaseException:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        pending = unpublished + ([event] if event is not None else [])
        if any(item['sequence'] != document['revision'] + index for index, item in enumerate(pending, 1)):
            raise ManifestError(f'{path} and its ledger receipts disagree; explicit review is required')
        if pending:
            document = {**document, 'events': document['events'] + pending,
                        'revision': document['revision'] + len(pending)}
            _write(path, document)
    sequence = event['sequence'] if event is not None else replay[1]
    return {'project': project['project_key'], 'id': job_id, 'revision': document['revision'],
            'event': document['events'][sequence - 1], 'current': current(document['events']),
            'request_uuid': request, 'replayed': event is None}
