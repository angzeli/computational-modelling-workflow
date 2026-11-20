"""Read-only integrity check of a native manifest project (#41 Phase 3b, #71).

Runs on the project owner and verifies the files against their permanent
anchors: job.json bytes against the integrity journal's ``manifest`` receipt,
registrations against the local queue and ``state.json``, annotations against
their ledger receipts, and recorded input/restart digests against the files
still present. It never writes, locks or repairs project data (SQLite may
leave its own transient -wal/-shm files beside the read-only ledger). A
read-only bit is not tamper proof and this detects accidental edits relative
to trusted owner records, not a malicious actor controlling both.

Backfilled legacy jobs (#76) are found beside their bound queue record (its
working directory or run-output directory), never by searching. Their job.json
is anchored like a native one; their frozen state.json has no binding or writer
and must stay read-only. A queue that cannot be resolved here leaves the job
unverifiable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .manifest_legacy import QUALIFIED, VOLUME_UUID, order_key, resolve
from .manifests import (KEY, LEDGER, MANIFEST_DIR, NATIVE_ID, NODE_UUID, SCHEMA_VERSION, ManifestError,
                        journal_prefix, load_project)

VASP_INPUTS = ('INCAR', 'POSCAR', 'KPOINTS')


class _Job:
    def __init__(self, job_id):
        self.id, self.findings, self.details = job_id, [], {}

    def error(self, code, message):
        self.findings.append({'level': 'error', 'code': code, 'message': message})

    def notice(self, code, message):
        self.findings.append({'level': 'notice', 'code': code, 'message': message})

    def result(self, reserved):
        errors = any(item['level'] == 'error' for item in self.findings)
        status = 'failed' if errors else ('verified' if 'job_sha256' in self.details else
                                          'reserved' if reserved else 'unverifiable')
        return {'id': self.id, 'status': status, **self.details, 'findings': self.findings}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _ledger_rows(root):
    """Reservations and annotation receipts, read without creating anything."""
    path = root / LEDGER
    if not path.is_file():
        raise ManifestError(f'Project ID ledger is missing: {path}')
    con = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    try:
        meta = dict(con.execute('SELECT key, value FROM meta'))
        reserved = {row[0] for row in con.execute('SELECT number FROM reservations')}
        receipts = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotation_requests'").fetchone():
            for job_id, event in con.execute('SELECT job_id, event FROM annotation_requests ORDER BY sequence'):
                receipts.setdefault(job_id, []).append(json.loads(event))
        return meta, reserved, receipts
    except sqlite3.Error as exc:
        raise ManifestError(f'Project ID ledger unreadable: {exc}') from exc
    finally:
        con.close()


def _real_directory(path):
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return None


def _load_json(job, path, code):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            job.error(code, f'{path.name} is not a regular file')
            return None
        document = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        job.error(code, f'{path.name} is unreadable: {exc}')
        return None
    if not isinstance(document, dict):  # An existing non-object is damage, never "missing".
        job.error(code, f'{path.name} is not a JSON object')
        return None
    return document


def _identity_pair(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value))


def _malformed_binding(payload):
    binding = payload.get('binding')
    if (set(payload) != {'node', 'queue', 'binding'}
            or not all(isinstance(payload[key], str) and KEY.fullmatch(payload[key]) for key in ('node', 'queue'))
            or not isinstance(binding, dict)
            or set(binding) != {'queue_id', 'state_root', 'root_identity', 'database_identity', 'volume_uuid'}
            or not (binding['queue_id'] is None or isinstance(binding['queue_id'], str)
                    and NODE_UUID.fullmatch(binding['queue_id']))
            or not isinstance(binding['state_root'], str) or not binding['state_root'].startswith('/')
            or not _identity_pair(binding['root_identity']) or not _identity_pair(binding['database_identity'])
            or not isinstance(binding['volume_uuid'], str) or not VOLUME_UUID.fullmatch(binding['volume_uuid'])):
        return 'is a malformed queue_binding receipt'
    return None


def _malformed(record):
    """Why a sequence-valid journal record cannot be indexed as a receipt, or None."""
    payload = record.get('payload')
    if record.get('event') not in ('manifest', 'registration', 'queue_binding', 'legacy_alias') or \
            not isinstance(payload, dict):
        return 'has an unknown event or a non-object payload'
    if record['event'] == 'queue_binding':
        return _malformed_binding(payload)
    if record['event'] == 'legacy_alias':
        if (set(payload) != {'label', 'target'} or not isinstance(payload['label'], str) or not payload['label']
                or not isinstance(payload['target'], str) or not QUALIFIED.fullmatch(payload['target'])):
            return 'is a malformed legacy_alias receipt'
        return None
    digest, job_id = payload.get('job_sha256'), payload.get('id')
    # Only backfilled legacy jobs carry a queue-qualified ID, and they are never registered.
    known = isinstance(job_id, str) and (NATIVE_ID.fullmatch(job_id) or record['event'] == 'manifest'
                                         and QUALIFIED.fullmatch(job_id))
    if not known or not isinstance(digest, str) or len(digest) != 64:
        return 'has a malformed job ID or job.json digest'
    binding = payload.get('binding')
    if record['event'] == 'manifest' and binding is not None:
        return 'is a manifest receipt with a binding'
    if record['event'] == 'registration' and not (
            isinstance(binding, dict) and isinstance(binding.get('job_id'), (int, str))
            and all(isinstance(binding.get(key), str) for key in ('node_id', 'queue_id', 'attempt_id'))):
        return 'is a registration receipt with a malformed binding'
    return None


def _identity(job, document, name, project_key, provenance='native'):
    expected = {'schema_version': SCHEMA_VERSION, 'id': job.id, 'project': project_key, 'provenance': provenance}
    actual = {key: document.get(key) for key in expected} if isinstance(document, dict) else None
    if actual != expected:
        job.error(f'{name}_identity', f'{name} identity {actual} differs from {expected}')
        return False
    return True


def _job_json(job, sidecar, anchors, project, provenance='native'):
    path = sidecar / 'job.json'
    if len(anchors) > 1:
        job.error('duplicate_manifest', f'{len(anchors)} manifest receipts anchor {job.id}')
    if not anchors:
        if os.path.lexists(path):
            job.error('unanchored', 'job.json has no manifest receipt; it is unverifiable, not accepted')
        return None
    anchor = anchors[0]['payload']['job_sha256']
    try:
        info = path.lstat()
    except FileNotFoundError:
        job.error('job_json_missing', 'job.json is missing although its manifest receipt exists')
        return None
    if not stat.S_ISREG(info.st_mode):
        job.error('job_json_not_file', 'job.json is not a regular file')
        return None
    if info.st_mode & 0o222:
        job.error('job_json_writable', f'job.json is writable (mode {stat.S_IMODE(info.st_mode):o})')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != anchor:
        job.error('job_json_changed', f'job.json bytes ({digest[:12]}) differ from its manifest receipt '
                                      f'({anchor[:12]}); it was edited after anchoring')
        return None
    job.details['job_sha256'] = digest
    document = json.loads(raw)
    owner = project['owner_node_id'] if provenance == 'native' else None  # Legacy node map deferred (#76).
    if _identity(job, document, 'job.json', project['project_key'], provenance) and \
            document.get('id_owner_node') != owner:
        job.error('job_json_owner', 'job.json names a different ID owner node')
    return document


def _registration(job, store, registrations, digest, project):
    if not registrations:
        job.notice('unregistered', 'No registration receipt: the ID is burned or registration is incomplete')
        return None
    if len(registrations) > 1:
        job.error('duplicate_registration', f'{len(registrations)} registration receipts for {job.id}')
    payload = registrations[0]['payload']
    if payload['job_sha256'] != digest:
        job.error('registration_digest', 'Registration receipt carries a different job.json digest')
    binding = payload['binding']
    job.details['binding'] = binding
    local = (store.queue_identity() or {})
    if (binding['node_id'], binding['queue_id']) != (local.get('node_id'), local.get('queue_id')):
        job.notice('remote', 'Registered on another node or queue; its state.json lives there')
        return binding, False
    try:
        with store.read_transaction() as con:
            record = store.get(con, binding['job_id'])
    except Exception as exc:  # The queue is the authority; a missing record is a finding, not a crash.
        job.error('queue_record_missing', f'Registered queue job {binding["job_id"]} is not readable here: {exc}')
        return binding, True
    manifest = record.get('manifest') or {}
    if (record['attempt_id'], manifest.get('id'), manifest.get('job_sha256'), manifest.get('project')) != (
            binding['attempt_id'], job.id, digest, project['project_key']):
        job.error('queue_record_binding', 'Queue record attempt or manifest differs from the registration receipt')
    job.details['queue_status'] = record['status']
    return binding, True


def _state(job, sidecar, binding, project):
    state = _load_json(job, sidecar / 'state.json', 'state_unreadable')
    if state is None:
        if not any(item['code'] == 'state_unreadable' for item in job.findings):
            job.notice('state_missing', 'No state.json has been published yet')
        return None
    if _identity(job, state, 'state.json', project['project_key']):
        if state.get('binding') != binding or state.get('writer_node') != binding['node_id']:
            job.error('state_binding', 'state.json is bound to a different attempt or writer than its registration')
        job.details.update(state=state.get('status'), scf_result=state.get('scf_result'),
                           hashes_pending=state.get('hashes_pending'))
    return state


def _annotations(job, sidecar, receipts, project, provenance='native'):
    from .manifest_annotations import _load, current
    path = sidecar / 'annotations.json'
    if not os.path.lexists(path) and not receipts:
        return
    try:
        document = _load(path, project['project_key'], job.id, provenance)
    except ManifestError as exc:
        job.error('annotations_inconsistent', str(exc))
        return
    published = document['revision']
    if receipts[:published] != document['events']:
        job.error('annotations_changed', 'annotations.json differs from its ledger receipts')
    else:
        if len(receipts) > published:
            job.notice('annotations_unpublished', f'{len(receipts) - published} committed annotation(s) await '
                                                  'publication by the next append')
        job.details['annotations'] = current(document['events'])  # Only receipt-matched events are shown.
    job.details['annotation_revision'] = published


def _inputs(job, directory, document, rehash):
    inputs = directory / 'inputs'
    for name, recorded in (document.get('inputs') or {}).items():
        if recorded is None:
            continue
        try:
            current = _sha256(inputs / name)
        except OSError:
            job.error('input_missing', f'inputs/{name} is missing or unreadable')
            continue
        if current != recorded:
            job.error('input_changed', f'inputs/{name} differs from its job.json digest')
    restart = {}
    for role, recorded in (document.get('restart_inputs') or {}).items():
        path = inputs / role
        if not path.is_file():
            restart[role] = 'missing'
            job.error('restart_input_missing', f'Restart input {role} is missing from inputs/')
        elif not rehash:
            restart[role] = 'present'
        elif _sha256(path) == recorded:
            restart[role] = 'unchanged'
        else:
            restart[role] = 'changed'
            job.error('restart_input_changed', f'Restart input {role} differs from its job.json digest')
    if restart:
        job.details['restart_inputs'] = restart


def _lineage(job, root, document, project, locate=None):
    parent = document.get('parent')
    if parent is None:
        return
    lineage = {'parent': parent, 'roles': {}}
    job.details['lineage'] = lineage
    state, path = None, None
    if isinstance(parent, str) and NATIVE_ID.fullmatch(parent):
        path = root / 'jobs' / parent / MANIFEST_DIR / 'state.json'
    elif locate is not None and isinstance(parent, str) and QUALIFIED.fullmatch(parent):
        sidecar = locate(parent)[0]
        path = sidecar / 'state.json' if sidecar is not None else None
    if path is not None:
        try:
            state = json.loads(path.read_bytes())
        except (OSError, ValueError):
            state = None
    outputs = (state or {}).get('restart_outputs') or {}
    for role, recorded in (document.get('restart_inputs') or {}).items():
        digest = outputs.get(role)
        if digest is None:
            lineage['roles'][role] = 'pending' if (state or {}).get('hashes_pending') else 'unavailable'
        elif digest == recorded:
            lineage['roles'][role] = 'match'
        else:
            lineage['roles'][role] = 'differs'
            job.error('parent_output_differs', f'{role} copy digest differs from parent {parent} output digest')


def _backfilled_state(job, sidecar, project):
    path = sidecar / 'state.json'
    state = _load_json(job, path, 'state_unreadable')
    if state is None:
        if not any(item['code'] == 'state_unreadable' for item in job.findings):
            job.error('state_missing', 'The backfilled state.json is missing')
        return
    if _identity(job, state, 'state.json', project['project_key'], 'backfilled'):
        if state.get('binding') is not None or state.get('writer_node') is not None:
            job.error('state_binding', 'Backfilled state.json names a binding or writer; legacy node identities '
                                       'are not mapped')
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode & 0o222:
            job.error('state_writable', f'Backfilled state.json is writable (mode {mode:o}); it is frozen evidence')
        job.details.update(state=state.get('status'), scf_result=state.get('scf_result'),
                           hashes_pending=state.get('hashes_pending'))


def _sha256_or_none(path):
    try:
        return _sha256(path) if stat.S_ISREG(path.lstat().st_mode) else None
    except OSError:
        return None


def _named(path):
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return document.get('id') if isinstance(document, dict) else None


def _locator(root, by_event, volume_uuid_of):
    """Find a backfilled job's sidecar beside its bound queue record: (sidecar, code, message)."""
    from .manifest_backfill import _route
    cache = {}

    def locate(job_id):
        if job_id in cache:
            return cache[job_id]
        try:
            record = resolve(root, job_id, volume_uuid_of=volume_uuid_of)['job']
        except ManifestError as exc:
            cache[job_id] = (None, 'legacy_unresolved', f'Its legacy queue record cannot be verified here: {exc}')
            return cache[job_id]
        route = _route(record) if str(record.get('engine')).upper() == 'VASP' else None
        wrappers = dict.fromkeys([Path(record['cwd'])] + ([route[2].parent] if route else []))
        sidecars = [wrapper / MANIFEST_DIR for wrapper in wrappers
                    if os.path.lexists(wrapper / MANIFEST_DIR / 'job.json')]
        digests = {anchor['payload']['job_sha256'] for anchor in by_event.get(('manifest', job_id), [])}
        chosen = ([sidecar for sidecar in sidecars if _sha256_or_none(sidecar / 'job.json') in digests]
                  or [sidecar for sidecar in sidecars if _named(sidecar / 'job.json') == job_id])
        if len(chosen) == 1:
            cache[job_id] = (chosen[0], None, None)
        elif not chosen:
            cache[job_id] = (None, 'job_json_missing', 'The manifest receipt exists but no sidecar beside its queue '
                                                       'record (working or run-output directory) holds this job')
        else:
            cache[job_id] = (None, 'sidecar_ambiguous', f'{len(chosen)} sidecars beside its queue record claim it')
        return cache[job_id]
    return locate


def _backfilled(job, root, project, anchors, receipts, labels, locate):
    if not anchors:
        job.error('not_backfilled', 'No manifest receipt anchors this legacy ID in this project')
        return
    sidecar, code, message = locate(job.id)
    if sidecar is None:
        (job.notice if code == 'legacy_unresolved' else job.error)(code, message)
        return
    document = _job_json(job, sidecar, anchors, project, 'backfilled')
    if document is None:
        return
    job.details.update(engine=document.get('engine'), parent=document.get('parent'), labels=labels,
                       sidecar=str(sidecar))
    _backfilled_state(job, sidecar, project)
    _annotations(job, sidecar, receipts, project, 'backfilled')
    _lineage(job, root, document, project, locate)


def _queue_bindings(records):
    """Each queue token and each queue database is bound at most once (#76)."""
    findings, tokens, databases = [], {}, {}
    for record in records:
        if record['event'] != 'queue_binding':
            continue
        payload, sequence = record['payload'], record['sequence']
        token = f'{payload["node"]}/{payload["queue"]}'
        database = (payload['binding']['volume_uuid'], payload['binding']['database_identity'][1])
        for seen, key, what in ((tokens, token, f'Queue {token}'), (databases, database, 'A queue database')):
            if key in seen:
                findings.append({'level': 'error', 'code': 'queue_binding_conflict',
                                 'message': f'{what} is bound again in journal record {sequence} '
                                            f'(first in record {seen[key]})'})
            seen.setdefault(key, sequence)
    return findings


def check(store, root, job_ids=(), *, rehash=False, volume_uuid_of=None):
    """Verify each requested native or backfilled job (all known ones by default) without writing anything."""
    root, project = load_project(root)
    key = project['project_key']
    meta, reserved, receipts = _ledger_rows(root)
    project_findings = []
    if (meta.get('project_key'), meta.get('owner_node_id')) != (key, project['owner_node_id']):
        project_findings.append({'level': 'error', 'code': 'ledger_identity',
                                 'message': 'Project ID ledger belongs to a different project or owner'})
    identity = store.queue_identity() or {}
    if identity.get('node_id') != project['owner_node_id']:
        project_findings.append({'level': 'notice', 'code': 'not_owner',
                                 'message': 'This node is not the project owner; remote state and queues are not visible'})
    records, damage = journal_prefix(root)
    for index, record in enumerate(records):
        reason = _malformed(record)
        if reason:  # Only the well-formed prefix before it is indexed, like any other journal damage.
            records, damage = records[:index], f'Integrity journal record {index + 1} {reason}'
            break
    if damage is not None:
        project_findings.append({'level': 'error', 'code': 'journal_damaged',
                                 'message': f'{damage}; {len(records)} complete record(s) were checked'})
    for record in records:
        if record.get('schema_version') != SCHEMA_VERSION or record.get('project') != key:
            project_findings.append({'level': 'error', 'code': 'journal_identity',
                                     'message': f'Journal record {record["sequence"]} names another project or schema'})
    project_findings += _queue_bindings(records)
    by_event, labels = {}, {}
    for record in records:
        if record['event'] == 'legacy_alias':
            labels.setdefault(record['payload']['target'], []).append(record['payload']['label'])
        elif record['event'] != 'queue_binding':
            by_event.setdefault((record['event'], record['payload']['id']), []).append(record)
    for target in sorted(set(labels) - {job_id for event, job_id in by_event if event == 'manifest'}, key=order_key):
        project_findings.append({'level': 'notice', 'code': 'alias_unanchored',
                                 'message': f'Legacy alias receipts name {target}, which no manifest receipt anchors'})
    locate = _locator(root, by_event, volume_uuid_of)
    jobs = root / 'jobs'
    present = {path.name for path in jobs.iterdir() if NATIVE_ID.fullmatch(path.name)} if jobs.is_dir() else set()
    known = present | {f'J{number}' for number in reserved} | {job_id for _, job_id in by_event if job_id}
    if job_ids:
        selected = list(dict.fromkeys(job_ids))
        invalid = [job_id for job_id in selected
                   if not (NATIVE_ID.fullmatch(str(job_id)) or QUALIFIED.fullmatch(str(job_id)))]
        if invalid:
            raise ManifestError('Manifest check takes native project IDs (J<n>) or backfilled queue-qualified '
                                f'IDs (<node>/<queue>:J<n>.<m>): {", ".join(map(str, invalid))}')
    else:
        selected = sorted(known, key=order_key)
    results = []
    for job_id in selected:
        job = _Job(job_id)
        if QUALIFIED.fullmatch(job_id):
            _backfilled(job, root, project, by_event.get(('manifest', job_id), []), receipts.get(job_id, []),
                        sorted(labels.get(job_id, [])), locate)
            results.append(job.result(False))
            continue
        number = int(job_id[1:])
        directory = jobs / job_id
        if number not in reserved:
            job.error('unreserved', f'{job_id} was never reserved in this project ledger')
        kind = _real_directory(directory)
        if kind is False:
            job.error('job_directory', f'{directory} is not a real directory')
        sidecar = directory / MANIFEST_DIR
        sidecar_kind = _real_directory(sidecar) if kind else None
        if sidecar_kind is False:
            job.error('sidecar', f'{sidecar} is not a real directory')
        anchors = by_event.get(('manifest', job_id), [])
        document = _job_json(job, sidecar, anchors, project) if sidecar_kind else None
        if anchors and not sidecar_kind and kind is not False:
            job.error('job_json_missing', 'The manifest receipt exists but the job has no sidecar on this owner')
        if document is not None:
            job.details.update(engine=document.get('engine'), parent=document.get('parent'))
            binding = _registration(job, store, by_event.get(('registration', job_id), []),
                                    job.details['job_sha256'], project)
            if binding is not None and binding[1]:
                _state(job, sidecar, binding[0], project)
                if document.get('engine') == 'VASP':
                    _inputs(job, directory, document, rehash)
                    _lineage(job, root, document, project)
            _annotations(job, sidecar, receipts.get(job_id, []), project)
        results.append(job.result(number in reserved))
    errors = [item for item in project_findings if item['level'] == 'error']
    errors += [item for job in results for item in job['findings'] if item['level'] == 'error']
    return {'project': key, 'root': str(root), 'status': 'FAILED' if errors else 'VERIFIED',
            'errors': len(errors), 'findings': project_findings, 'jobs': results}


def summary_text(result):
    lines = [f'{result["project"]}: {result["status"]} ({result["errors"]} error(s)) at {result["root"]}']
    lines += [f'  {item["level"]}: {item["message"]}' for item in result['findings']]
    for job in result['jobs']:
        lines.append(f'{job["id"]}: {job["status"]}')
        lines += [f'  {item["level"]} {item["code"]}: {item["message"]}' for item in job['findings']]
    return '\n'.join(lines)
