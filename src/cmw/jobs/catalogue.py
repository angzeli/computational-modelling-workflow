"""Owner-local, additive research catalogue in the existing Jobs database."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import sqlite3
import stat
import time
import unicodedata

from .catalogue_models import CataloguePage, CatalogueQuery, CatalogueRecord, FIELDS, tags_value, text_value
from .store import JobsError, Store

SCHEMA = 1
QUERY_SECONDS = 2.0
PAGE_BYTES = 512 * 1024
_UNSET = object()


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _literal(value):
    return unicodedata.normalize('NFC', value or '').casefold()


def _revision(value):
    if type(value) is not int or value < 0:
        raise JobsError('Expected catalogue revision must be a nonnegative integer')


class CatalogueStore:
    """No catalogue table is created until an explicit, validated mutation."""
    def __init__(self, store):
        self.store = store if isinstance(store, Store) else Store(store)

    @staticmethod
    def _tables(con):
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _check(self, con):
        tables = self._tables(con)
        if not tables:
            return None
        control = self.store.control(con)
        if control['schema'] not in {1, 2, 3, 4}:
            raise JobsError('Unsupported Jobs state schema')
        identity = self.store._queue_identity(con, control['schema'])
        if 'catalogue_state' in tables:
            row = con.execute('SELECT schema FROM catalogue_state WHERE id=1').fetchone()
            if row is None or row[0] != SCHEMA:
                raise JobsError('Unsupported catalogue storage schema')
            if 'catalogue_annotations' not in tables or 'catalogue_requests' not in tables:
                raise JobsError('Catalogue storage tables are incomplete')
            if con.execute("SELECT 1 FROM catalogue_annotations a LEFT JOIN jobs j ON j.id=a.job_id "
                    "WHERE j.id IS NULL OR a.attempt_id != json_extract(j.data,'$.attempt_id') LIMIT 1").fetchone():
                raise JobsError('Catalogue attempt identity differs')
        elif {'catalogue_annotations', 'catalogue_requests', 'catalogue_relations'} & tables:
            raise JobsError('Catalogue storage schema is missing')
        return identity

    @contextmanager
    def read_connection(self):
        if not self.store.path.exists():
            yield None
            return
        con = None
        try:
            before = self.store.path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise JobsError('Queue database must be a regular file')
            con = sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True, timeout=2,
                                  isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('PRAGMA locking_mode=EXCLUSIVE')
            con.execute('BEGIN')
            if con.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise JobsError('Unsupported database journal format for read-only catalogue')
            self._check(con)
            after = self.store.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise JobsError('Queue database changed while opening catalogue')
            yield con
            after = self.store.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise JobsError('Queue database changed during catalogue read')
        except sqlite3.Error as exc:
            raise JobsError(f'Catalogue database unavailable: {exc}') from exc
        finally:
            if con is not None:
                con.close()

    @contextmanager
    def transaction(self):
        """Explicit metadata writes never initialize queues or migrate Jobs schema."""
        try:
            self.store.ensure_private_root(create=False)
            if not stat.S_ISREG(self.store.path.lstat().st_mode):
                raise JobsError('Queue database must be a regular file')
        except OSError as exc:
            raise JobsError(f'Catalogue queue unavailable: {exc}') from exc
        con = None
        try:
            con = sqlite3.connect(self.store.path.as_uri() + '?mode=rw', uri=True, timeout=2,
                                  isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute('BEGIN IMMEDIATE')
            self._check(con)
            if json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])['schema'] == 1:
                raise JobsError('Legacy schema-1 metadata is read-only; explicitly qualify a current Jobs runtime first')
            yield con
            con.commit()
        except sqlite3.Error as exc:
            if con is not None:
                con.rollback()
            raise JobsError(f'Catalogue database unavailable: {exc}') from exc
        except BaseException:
            if con is not None:
                con.rollback()
            raise
        finally:
            if con is not None:
                con.close()

    @staticmethod
    def ensure_tables(con):
        con.execute('CREATE TABLE IF NOT EXISTS catalogue_state (id INTEGER PRIMARY KEY, schema INTEGER NOT NULL)')
        con.execute('INSERT OR IGNORE INTO catalogue_state VALUES (1, ?)', (SCHEMA,))
        con.execute('CREATE TABLE IF NOT EXISTS catalogue_annotations ('
                    'job_id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, '
                    'revision INTEGER NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS catalogue_requests ('
                    'request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL)')

    def metadata(self, con, job):
        result = {key: '' for key in FIELDS[:-1]}
        result.update(tags=[], revision=0, updated_at=None)
        if 'catalogue_annotations' not in self._tables(con):
            return result
        row = con.execute('SELECT attempt_id, revision, data, updated_at FROM catalogue_annotations WHERE job_id=?',
                          (job['id'],)).fetchone()
        if row is None:
            return result
        if row['attempt_id'] != job['attempt_id']:
            raise JobsError('Catalogue attempt identity differs')
        try:
            data = json.loads(row['data'])
            if not isinstance(data, dict) or set(data) != set(FIELDS):
                raise ValueError('Unknown catalogue metadata fields')
            data = self.validate_patch(data)
            _revision(row['revision'])
            result.update(data, revision=row['revision'], updated_at=row['updated_at'])
        except (ValueError, TypeError, KeyError) as exc:
            raise JobsError(f'Invalid catalogue metadata: {exc}') from exc
        return result

    @staticmethod
    def validate_patch(patch):
        if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
            raise JobsError('Catalogue patch requires only alias, project, system, series, research_stage or tags')
        try:
            return {key: list(tags_value(value)) if key == 'tags' else text_value(value, key)
                    for key, value in patch.items()}
        except ValueError as exc:
            raise JobsError(str(exc)) from exc

    def bump_metadata(self, con, job, metadata=None):
        current = self.metadata(con, job)
        values = current if metadata is None else metadata
        self.ensure_tables(con)
        revision, updated_at = current['revision'] + 1, time.time()
        data = {key: values[key] for key in FIELDS}
        con.execute('INSERT INTO catalogue_annotations(job_id,attempt_id,revision,data,updated_at) VALUES (?,?,?,?,?) '
                    'ON CONFLICT(job_id) DO UPDATE SET revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at',
                    (job['id'], job['attempt_id'], revision, _canonical(data), updated_at))
        return {**data, 'revision': revision, 'updated_at': updated_at}

    @staticmethod
    def request_lookup(con, request_id, operation, digest):
        import re
        if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise JobsError('Catalogue request ID must be a lowercase UUID hex identity')
        if 'catalogue_requests' not in CatalogueStore._tables(con):
            return None
        row = con.execute('SELECT operation,digest,result FROM catalogue_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        if (row['operation'], row['digest']) != (operation, digest):
            raise JobsError('Catalogue request ID already belongs to a different operation')
        return json.loads(row['result'])

    @staticmethod
    def request_record(con, request_id, operation, digest, result_json):
        CatalogueStore.ensure_tables(con)
        value = json.loads(result_json) if isinstance(result_json, str) else result_json
        con.execute('INSERT INTO catalogue_requests VALUES (?,?,?,?)',
                    (request_id, operation, digest, _canonical(value)))

    def record(self, con, job, *, identity=_UNSET, preview=False):
        if identity is _UNSET:
            identity = self._check(con)
        metadata = self.metadata(con, job)
        return CatalogueRecord(schema=1,
            node_id=identity['node_id'] if identity else None, queue_id=identity['queue_id'] if identity else None,
            job_id=job['id'], attempt_id=job['attempt_id'], display_id=job['display_id'][:256],
            name=job['name'][:512 if preview else 4096], engine=job['engine'][:256], status=job['status'],
            note=job.get('note', '')[:512 if preview else 4096], note_revision=job.get('note_revision', 0),
            order=job.get('order'), **{key: metadata[key] for key in FIELDS[:-1]},
            tags=tuple(metadata['tags']), revision=metadata['revision'], updated_at=metadata['updated_at'])

    def get_job(self, con, job_id):
        # Wire targets carry internal integers, never mutable display aliases.
        if type(job_id) is int:
            row = con.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise JobsError(f'No such job: {job_id}')
            return json.loads(row[0])
        if not isinstance(job_id, str):
            raise JobsError('Invalid job identity')
        return self.store.get(con, job_id)

    def read(self, job_id, *, expected_attempt_id=None):
        with self.read_connection() as con:
            if con is None:
                raise JobsError(f'No such job: {job_id}')
            job = self.get_job(con, job_id)
            if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:
                raise JobsError('Attempt identity differs')
            return self.record(con, job)

    def edit(self, job_id, patch, *, expected_revision, expected_attempt_id, request_id=None):
        _revision(expected_revision)
        patch = self.validate_patch(patch)
        if not isinstance(expected_attempt_id, str) or not expected_attempt_id:
            raise JobsError('Catalogue edit requires an immutable attempt identity')
        with self.transaction() as con:
            job = self.get_job(con, job_id)
            if job['attempt_id'] != expected_attempt_id:
                raise JobsError('Attempt identity differs')
            identity = self._check(con)
            digest = hashlib.sha256(_canonical({'job_id': job['id'], 'attempt_id': expected_attempt_id,
                'identity': identity, 'expected_revision': expected_revision, 'patch': patch}).encode()).hexdigest()
            if request_id is not None:
                prior = self.request_lookup(con, request_id, 'catalogue.edit', digest)
                if prior is not None:
                    prior['tags'] = tuple(prior['tags'])
                    return CatalogueRecord(**prior)
            metadata = self.metadata(con, job)
            if metadata['revision'] != expected_revision:
                raise JobsError('Catalogue changed since editing began; reload before saving')
            if any(metadata[key] != value for key, value in patch.items()):
                metadata.update(patch)
                self.bump_metadata(con, job, metadata)
            result = self.record(con, job, identity=identity)
            if request_id is not None:
                self.request_record(con, request_id, 'catalogue.edit', digest, asdict(result))
            return result

    def _generation(self, con, identity, check):
        # Bind cursor validity to the same SQLite read snapshot as its rows.
        # Controller/worker heartbeat writes do not change this projection.
        info = self.store.path.stat()
        digest = hashlib.sha256(_canonical((info.st_dev, info.st_ino, identity)).encode())
        annotated = 'catalogue_annotations' in self._tables(con)
        join = (" LEFT JOIN catalogue_annotations a ON a.job_id=j.id AND a.attempt_id=json_extract(j.data,'$.attempt_id')"
                if annotated else '')
        metadata = 'a.data, a.revision, a.updated_at' if annotated else 'NULL, NULL, NULL'
        fields = ','.join(f"json_extract(j.data,'$.{key}')" for key in
            ('attempt_id', 'display_id', 'name', 'engine', 'status', 'note', 'note_revision', 'order'))
        for row in con.execute(f'SELECT j.id,{fields},{metadata} FROM jobs j{join} ORDER BY j.id'):
            if reason := check():
                raise JobsError(reason)
            digest.update(_canonical(tuple(row)).encode())
            digest.update(b'\n')
        return digest.hexdigest()

    @staticmethod
    def _cursor(query, generation, offset):
        params = asdict(query)
        params.pop('cursor')
        digest = hashlib.sha256(_canonical(params).encode()).hexdigest()
        return base64.urlsafe_b64encode(_canonical({'schema': 1, 'generation': generation,
            'query': digest, 'offset': offset}).encode()).decode()

    @classmethod
    def _offset(cls, query, generation):
        if query.cursor is None:
            return 0
        try:
            value = json.loads(base64.b64decode(query.cursor.encode(), altchars=b'-_', validate=True))
            if (type(value) is not dict or set(value) != {'schema', 'generation', 'query', 'offset'}
                    or type(value['offset']) is not int or value['offset'] < 0
                    or query.cursor != cls._cursor(query, generation, value['offset'])):
                raise ValueError()
            return value['offset']
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise JobsError('Catalogue cursor invalidated; restart this queue query') from exc

    def query(self, query=None, *, cancelled=None):
        query = query or CatalogueQuery()
        if not isinstance(query, CatalogueQuery):
            raise JobsError('Catalogue query requires a validated CatalogueQuery')
        began = time.monotonic()
        def check():
            if cancelled is not None and (cancelled.is_set() if hasattr(cancelled, 'is_set') else cancelled()):
                return 'Catalogue query cancelled'
            if time.monotonic() - began > QUERY_SECONDS:
                return 'Catalogue query exceeded its two-second metadata read bound; narrow the query'
            return None
        with self.read_connection() as con:
            if check():
                raise JobsError(check())
            if con is None or 'jobs' not in self._tables(con):
                if query.cursor is not None:
                    raise JobsError('Catalogue cursor invalidated; restart this queue query')
                return CataloguePage(1, None, None, (), None, 'empty', 0, 0, (), 'EMPTY', time.time())
            con.set_progress_handler(lambda: int(check() is not None), 1000)
            con.create_function('cmw_literal_contains', 2, lambda haystack, needle: int(_literal(needle) in _literal(haystack)),
                                deterministic=True)
            con.create_function('cmw_literal_key', 1, _literal, deterministic=True)
            identity = self._check(con)
            node_id, queue_id = ((identity['node_id'], identity['queue_id']) if identity else (None, None))
            generation = self._generation(con, identity, check)
            offset = self._offset(query, generation)
            tables = self._tables(con)
            annotated = 'catalogue_annotations' in tables
            join = (" LEFT JOIN catalogue_annotations a ON a.job_id=j.id AND a.attempt_id=json_extract(j.data,'$.attempt_id')"
                    if annotated else '')
            metadata = 'a.data' if annotated else "'{}'"
            clauses, params = [], []
            if query.node_id is not None and query.node_id != node_id or query.queue_id is not None and query.queue_id != queue_id:
                clauses.append('0')
            if query.text:
                clauses.append("cmw_literal_contains(coalesce(json_extract(j.data,'$.name'),'') || char(10) || "
                               "coalesce(json_extract(j.data,'$.note'),'') || char(10) || "
                               f"coalesce(json_extract({metadata},'$.alias'),''), ?)")
                params.append(query.text)
            for key in ('engine', 'status', 'project', 'system', 'series', 'research_stage'):
                value = getattr(query, key)
                if value is not None:
                    source = 'j.data' if key in {'engine', 'status'} else metadata
                    clauses.append(f"coalesce(json_extract({source},'$.{key}'),'')=?")
                    params.append(value)
            for tag in query.tags:
                clauses.append(f"EXISTS (SELECT 1 FROM json_each({metadata}, '$.tags') WHERE value=?)")
                params.append(tag)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            try:
                counts = tuple((row[0], row[1]) for row in con.execute(
                    "SELECT json_extract(data,'$.status'),count(*) FROM jobs GROUP BY json_extract(data,'$.status') ORDER BY 1"))
                matching = con.execute('SELECT count(*) FROM jobs j' + join + where, params).fetchone()[0]
                sort = {'id': 'j.id', 'id_desc': 'j.id DESC', 'name':
                    "cmw_literal_key(json_extract(j.data,'$.name')), j.id"}[query.sort]
                projection = ','.join(["j.id AS id"] + [
                    f"json_extract(j.data,'$.{key}') AS {key}" for key in
                    ('attempt_id', 'display_id', 'name', 'engine', 'status', 'note', 'note_revision')]
                    + ["json_extract(j.data,'$.order') AS queue_order"])
                rows = con.execute(f'SELECT {projection} FROM jobs j{join}{where} ORDER BY {sort} LIMIT ? OFFSET ?',
                                   [*params, query.limit, offset])
                records, byte_count = [], 0
                for row in rows:
                    job = dict(row)
                    job['order'] = job.pop('queue_order')
                    job['note'], job['note_revision'] = job['note'] or '', job['note_revision'] or 0
                    record = self.record(con, job, identity=identity, preview=True)
                    size = len(_canonical(asdict(record)).encode())
                    if records and byte_count + size > PAGE_BYTES:
                        break
                    records.append(record)
                    byte_count += size
                if check():
                    raise JobsError(check())
                next_offset = offset + len(records)
                cursor = self._cursor(query, generation, next_offset) if next_offset < matching else None
                return CataloguePage(1, node_id, queue_id, tuple(records), cursor, generation,
                    sum(count for _, count in counts), matching, counts,
                    'COMPLETE' if identity else 'LOCAL_UNENROLLED', time.time())
            except sqlite3.OperationalError as exc:
                if check():
                    raise JobsError(check()) from exc
                raise
