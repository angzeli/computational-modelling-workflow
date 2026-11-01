"""Explicit human attempt relationships, separate from workflow dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import re
from pathlib import Path
import stat
import time
from uuid import uuid4

from cmw.core.artifacts import artifact_from_dict
from cmw.core.provenance import stable_hash
from cmw.nodes.protocol import target_identity
from .store import JobsError, Store

KINDS = {'continuation', 'repeat', 'next-stage', 'comparison'}
MAX_RELATIONS = 100
RECORD_BYTES = 2 * 1024 * 1024
QUALIFICATION = ('Operator intent only; does not satisfy a workflow dependency, validate '
                 'a scientific result, or authorize execution.')
RECORD_SCOPE = ('Recorded artifact IDs, producer identities and parent edge verified at edit time; '
                'scientific output bytes and current result validity are NOT assessed. '
                'Editable local records are not authenticated evidence.')


def _target(value):
    try:
        return dict(target_identity(value))
    except ValueError as exc:
        raise JobsError(f'Invalid global relationship target: {exc}') from exc


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    kind: str
    parent_json: str
    basis: str
    resolution: str
    state: str
    created_at: float
    retracted_at: float | None
    evidence_json: str | None
    qualification: str

    def __post_init__(self):
        if (len(self.relation_id) != 32 or any(c not in '0123456789abcdef' for c in self.relation_id)
                or self.kind not in KINDS or self.basis not in {'intent', 'artifact-record'}
                or self.resolution not in {'LOCAL_PRESENT', 'UNRESOLVED_REMOTE', 'UNAVAILABLE_LOCAL'}
                or self.state not in {'ACTIVE', 'RETRACTED'} or not _timestamp(self.created_at)
                or (self.retracted_at is not None and not _timestamp(self.retracted_at))
                or (self.state == 'RETRACTED') != (self.retracted_at is not None)
                or (self.retracted_at is not None and self.retracted_at < self.created_at)
                or self.qualification not in {QUALIFICATION, RECORD_SCOPE}):
            raise ValueError('Invalid relationship record')
        if type(self.parent_json) is not str or len(self.parent_json) > 512:
            raise ValueError('Invalid relationship target encoding')
        _target(self.parent)
        if self.evidence_json is not None and (type(self.evidence_json) is not str or len(self.evidence_json) > 12288):
            raise ValueError('Invalid bounded evidence encoding')
        if self.basis == 'intent':
            if self.evidence is not None or self.qualification != QUALIFICATION:
                raise ValueError('Intent cannot carry verified artifact evidence')
        else:
            if (type(self.evidence) is not dict or set(self.evidence) != {
                    'parent_record', 'child_record', 'parent_artifact_id', 'child_artifact_id',
                    'parent_record_sha256', 'child_record_sha256', 'verified_at', 'scope'}
                    or self.evidence['scope'] != 'recorded-artifact-parent-edge'
                    or self.qualification != RECORD_SCOPE or not _timestamp(self.evidence['verified_at'])):
                raise ValueError('Invalid artifact relationship evidence')
            for key in ('parent_artifact_id', 'child_artifact_id', 'parent_record_sha256', 'child_record_sha256'):
                value = self.evidence[key]
                if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                    raise ValueError('Invalid artifact identity')
            for key in ('parent_record', 'child_record'):
                value = self.evidence[key]
                if type(value) is not str or len(value) > 4096 or not Path(value).is_absolute() or '\0' in value:
                    raise ValueError('Invalid evidence location')


    @property
    def parent(self):
        return json.loads(self.parent_json)

    @property
    def evidence(self):
        return None if self.evidence_json is None else json.loads(self.evidence_json)


@dataclass(frozen=True)
class RelationPage:
    schema: int
    node_id: str | None
    queue_id: str | None
    job_id: int
    attempt_id: str
    revision: int
    relations: tuple[RelationRecord, ...]
    coverage: str
    observed_at: float

    def __post_init__(self):
        if (type(self.schema) is not int or self.schema != 1 or type(self.job_id) is not int
                or self.job_id < 1 or type(self.revision) is not int or self.revision < 0
                or type(self.relations) is not tuple or len(self.relations) > MAX_RELATIONS
                or not all(type(x) is RelationRecord for x in self.relations)
                or self.coverage not in {'COMPLETE', 'LOCAL_UNENROLLED'} or not _timestamp(self.observed_at)
                or type(self.attempt_id) is not str or not re.fullmatch('[0-9a-f]{32}', self.attempt_id)):
            raise ValueError('Invalid relationship page')
        if self.node_id is not None or self.queue_id is not None:
            _target({k: getattr(self, k) for k in ('node_id', 'queue_id', 'job_id', 'attempt_id')})
        elif self.relations:
            raise ValueError('Unenrolled relationships cannot have global authority')
        if len({r.relation_id for r in self.relations}) != len(self.relations):
            raise ValueError('Duplicate relationship identity')
        if len(json.dumps(asdict(self), ensure_ascii=True, allow_nan=False).encode()) > 1024 * 1024:
            raise ValueError('Relationship history exceeds the 1 MiB wire-safe bound')


def _read_record(path, target, artifact_id):
    """Bounded metadata linkage, deliberately not a second scientific verifier."""
    if type(path) is not str or len(path) > 4096 or not Path(path).is_absolute():
        raise JobsError('Artifact records require explicit absolute owner-node paths')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > RECORD_BYTES:
            raise JobsError('Artifact record must be a regular file no larger than 2 MiB')
        raw = handle.read(RECORD_BYTES + 1)
        after = os.fstat(handle.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > RECORD_BYTES or identity(before) != identity(after) or identity(after) != identity(Path(path).stat()):
        raise JobsError('Artifact record changed while reading')
    record = json.loads(raw)
    if (type(record) is not dict or record.get('kind') != 'vasp-artifact-finalization'
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('publication', {}).get('state') != 'complete'
            or record.get('publication', {}).get('record_path') != str(Path(path).resolve())
            or record.get('record_id') != stable_hash({k: v for k, v in record.items()
                                                      if k not in {'record_id', 'publication', 'publication_id'}})):
        raise JobsError('Unsupported or inconsistent completed artifact record')
    execution = record.get('evidence', {}).get('binding', {}).get('execution', {})
    job = execution.get('job', {})
    enrollment = execution.get('binding', {}).get('queue_identity', {})
    actual = {'node_id': enrollment.get('node_id'), 'queue_id': enrollment.get('queue_id'),
              'job_id': job.get('id'), 'attempt_id': job.get('attempt_id')}
    if actual != target:
        raise JobsError('Artifact producer differs from the exact global attempt')
    bundle = record.get('artifact_bundle', {})
    values = bundle.get('artifacts', [])
    if (bundle.get('status') != 'FINALIZED' or type(values) is not list or not 1 <= len(values) <= 64
            or type(bundle.get('artifact_count')) is not int or bundle['artifact_count'] != len(values)):
        raise JobsError('Missing bounded finalized artifact bundle')
    matches = [value for value in values if type(value) is dict and value.get('artifact_id') == artifact_id]
    if len(matches) != 1:
        raise JobsError('Artifact identity is absent or ambiguous')
    artifact = artifact_from_dict(matches[0])
    if (artifact.artifact_id != artifact_id or not artifact.validation.passed
            or artifact.producing_calculation != record.get('producing_calculation')
            or artifact.protocol.get('operational_attempt_id') != target['attempt_id']):
        raise JobsError('Artifact content or producing attempt identity differs')
    return artifact, hashlib.sha256(raw).hexdigest()


def verify_record_link(evidence, parent, child):
    if type(evidence) is not dict or set(evidence) != {'parent_record', 'child_record', 'parent_artifact_id', 'child_artifact_id'}:
        raise JobsError('Artifact evidence requires parent/child record paths and artifact IDs')
    if (parent['node_id'], parent['queue_id']) != (child['node_id'], child['queue_id']):
        raise JobsError('Cross-owner evidence remains unresolved; remote record paths are never opened locally')
    try:
        pa, ph = _read_record(evidence['parent_record'], parent, evidence['parent_artifact_id'])
        ca, ch = _read_record(evidence['child_record'], child, evidence['child_artifact_id'])
        if pa.artifact_id not in ca.parent_artifacts:
            raise JobsError('Child artifact does not declare the selected parent artifact')
        return {**evidence, 'parent_record_sha256': ph, 'child_record_sha256': ch,
                'verified_at': time.time(), 'scope': 'recorded-artifact-parent-edge'}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, JobsError):
            raise
        raise JobsError(f'Artifact record linkage unavailable: {exc}') from exc


class RelationStore:
    def __init__(self, store):
        from .catalogue import CatalogueStore
        self.store = store
        self.catalogue = CatalogueStore(store)

    @staticmethod
    def _exists(con):
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalogue_relations'").fetchone() is not None

    def _identity(self, con):
        control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
        return self.store._queue_identity(con, control['schema'])

    def _job(self, con, job_id, expected_attempt_id):
        job = self.catalogue.get_job(con, job_id)
        if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:
            raise JobsError('Relationship attempt identity differs')
        return job

    def _page(self, con, job):
        identity = self._identity(con)
        rows = []
        if self._exists(con):
            rows = con.execute('SELECT data FROM catalogue_relations WHERE job_id=? AND attempt_id=? ORDER BY created_at, relation_id LIMIT ?',
                               (job['id'], job['attempt_id'], MAX_RELATIONS + 1)).fetchall()
        if len(rows) > MAX_RELATIONS:
            raise JobsError('Relationship history exceeds the supported bound')
        relations = []
        for row in rows:
            value = json.loads(row[0])
            parent = json.loads(value['parent_json'])
            if identity and (parent['node_id'], parent['queue_id']) == (identity['node_id'], identity['queue_id']):
                try:
                    self._job(con, parent['job_id'], parent['attempt_id'])
                    value['resolution'] = 'LOCAL_PRESENT'
                except JobsError:
                    value['resolution'] = 'UNAVAILABLE_LOCAL'
            else:
                value['resolution'] = 'UNRESOLVED_REMOTE'
            relations.append(RelationRecord(**value))
        return RelationPage(1, None if identity is None else identity['node_id'],
                            None if identity is None else identity['queue_id'], job['id'], job['attempt_id'],
                            self.catalogue.metadata(con, job)['revision'], tuple(relations),
                            'LOCAL_UNENROLLED' if identity is None else 'COMPLETE', time.time())

    def list(self, job_id, *, expected_attempt_id=None):
        with self.catalogue.read_connection() as con:
            if con is None:
                raise JobsError('Queue is unavailable')
            return self._page(con, self._job(con, job_id, expected_attempt_id))

    def _checked(self, con, job_id, expected_attempt_id, expected_revision):
        if not expected_attempt_id or type(expected_revision) is not int or expected_revision < 0:
            raise JobsError('Exact attempt and expected catalogue revision are required')
        job = self._job(con, job_id, expected_attempt_id)
        if self.catalogue.metadata(con, job)['revision'] != expected_revision:
            raise JobsError('Catalogue changed; reload before editing relationships')
        identity = self._identity(con)
        if identity is None:
            raise JobsError('Global attempt relationships require explicit existing queue enrollment')
        return job, {'node_id': identity['node_id'], 'queue_id': identity['queue_id'],
                     'job_id': job['id'], 'attempt_id': job['attempt_id']}

    def _no_cycle(self, con, child, parent):
        todo = [parent]
        visited = set()
        while todo:
            target = todo.pop()
            if target == child:
                raise JobsError('Continuation/repeat/next-stage relationship would create a cycle')
            if (target['node_id'], target['queue_id']) != (child['node_id'], child['queue_id']):
                continue
            key = (target['job_id'], target['attempt_id'])
            if key in visited:
                continue
            visited.add(key)
            if len(visited) > MAX_RELATIONS:
                raise JobsError('Relationship traversal exceeds bounded cycle verification')
            for row in con.execute('SELECT data FROM catalogue_relations WHERE job_id=? AND attempt_id=? LIMIT ?', (*key, MAX_RELATIONS + 1)):
                value = json.loads(row[0])
                if value['state'] == 'ACTIVE' and value['kind'] != 'comparison':
                    todo.append(json.loads(value['parent_json']))

    def add(self, job_id, *, parent, kind, expected_attempt_id, expected_revision, request_id=None, evidence=None):
        parent = _target(parent)
        if kind not in KINDS:
            raise JobsError('Unsupported relationship kind')
        digest = stable_hash(dict(job_id=job_id, parent=parent, kind=kind, expected_attempt_id=expected_attempt_id,
                                  expected_revision=expected_revision, evidence=evidence))
        with self.catalogue.transaction() as con:
            self.catalogue.ensure_tables(con)
            replay = None if request_id is None else self.catalogue.request_lookup(con, request_id, 'relation.add', digest)
            if replay is not None:
                return self._decode(replay)
            job, child = self._checked(con, job_id, expected_attempt_id, expected_revision)
            if child == parent or child['attempt_id'] == parent['attempt_id']:
                raise JobsError('An attempt cannot be related to itself or a conflicting identity alias')
            local = (child['node_id'], child['queue_id']) == (parent['node_id'], parent['queue_id'])
            if local:
                self._job(con, parent['job_id'], parent['attempt_id'])
            con.execute('CREATE TABLE IF NOT EXISTS catalogue_relations (relation_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, attempt_id TEXT NOT NULL, created_at REAL NOT NULL, data TEXT NOT NULL)')
            con.execute('CREATE INDEX IF NOT EXISTS catalogue_relation_job ON catalogue_relations(job_id,attempt_id)')
            existing = self._page(con, job).relations
            if len(existing) >= MAX_RELATIONS:
                raise JobsError('Relationship history is limited to 100 records per attempt')
            if any(r.state == 'ACTIVE' and r.kind == kind and r.parent == parent for r in existing):
                raise JobsError('Active relationship already exists')
            if kind != 'comparison':
                self._no_cycle(con, child, parent)
            proof = None if evidence is None else verify_record_link(evidence, parent, child)
            record = RelationRecord(uuid4().hex, kind, json.dumps(parent, sort_keys=True), 'intent' if proof is None else 'artifact-record',
                                    'LOCAL_PRESENT' if local else 'UNRESOLVED_REMOTE', 'ACTIVE', time.time(), None,
                                    None if proof is None else json.dumps(proof, sort_keys=True), QUALIFICATION if proof is None else RECORD_SCOPE)
            con.execute('INSERT INTO catalogue_relations VALUES (?,?,?,?,?)',
                        (record.relation_id, job['id'], job['attempt_id'], record.created_at, json.dumps(asdict(record))))
            self.catalogue.bump_metadata(con, job)
            page = self._page(con, job)
            if request_id is not None:
                self.catalogue.request_record(con, request_id, 'relation.add', digest, asdict(page))
            return page

    def retract(self, job_id, relation_id, *, expected_attempt_id, expected_revision, request_id=None):
        digest = stable_hash(dict(job_id=job_id, relation_id=relation_id, expected_attempt_id=expected_attempt_id,
                                  expected_revision=expected_revision))
        with self.catalogue.transaction() as con:
            self.catalogue.ensure_tables(con)
            replay = None if request_id is None else self.catalogue.request_lookup(con, request_id, 'relation.retract', digest)
            if replay is not None:
                return self._decode(replay)
            job, _ = self._checked(con, job_id, expected_attempt_id, expected_revision)
            matches = [r for r in self._page(con, job).relations if r.relation_id == relation_id]
            if len(matches) != 1 or matches[0].state != 'ACTIVE':
                raise JobsError('Active relationship not found on this exact attempt')
            data = {**asdict(matches[0]), 'state': 'RETRACTED', 'retracted_at': time.time()}
            con.execute('UPDATE catalogue_relations SET data=? WHERE relation_id=?', (json.dumps(data), relation_id))
            self.catalogue.bump_metadata(con, job)
            page = self._page(con, job)
            if request_id is not None:
                self.catalogue.request_record(con, request_id, 'relation.retract', digest, asdict(page))
            return page

    @staticmethod
    def _decode(value):
        if type(value) is str:
            value = json.loads(value)
        return RelationPage(**{**value, 'relations': tuple(RelationRecord(**r) for r in value['relations'])})
